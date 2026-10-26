"""Data quality checks per run, written to dq_checks.

'error' means the run's leads should not be trusted; 'warn' means look before relying on it.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

import duckdb

from shadow import db
from shadow.config import CATEGORIES, MATCH_THRESHOLD

log = logging.getLogger(__name__)

ERROR, WARN = "error", "warn"
MAX_LISTING_CHANGE = 0.3
MAX_GOOGLE_AGE_DAYS = 45
MAX_SODRA_LAG_MONTHS = 3
MIN_SODRA_MONTHS = 12
MIN_MONTH_ROWS_RATIO = 0.9
MIN_REGISTRY_ROWS = 400_000
MAX_REGISTRY_AGE_DAYS = 7
MIN_MATCH_RATE = 0.2
MAX_FLAGGED_SHARE = 0.15


@dataclass
class Result:
    check: str
    severity: str
    passed: bool
    value: float | None
    threshold: str
    details: str


def scalar(con: duckdb.DuckDBPyConnection, sql: str, *params):
    return con.execute(sql, list(params)).fetchone()[0]


def months_between(later: date, earlier: date) -> int:
    return (later.year - earlier.year) * 12 + later.month - earlier.month


def google_checks(con, run_id: str, today: date) -> list[Result]:
    n = scalar(con, "SELECT count(*) FROM google_listings WHERE run_id = ?", run_id)
    results = [Result("google.listings_present", ERROR, n > 0, n, "> 0", f"{n} listings in run")]
    if not n:
        return results

    prev = con.execute(
        """SELECT run_id, count(*) FROM google_listings
           WHERE run_id < ? GROUP BY 1 ORDER BY 1 DESC LIMIT 1""",
        [run_id],
    ).fetchone()
    if prev:
        change = abs(n - prev[1]) / prev[1]
        results.append(Result(
            "google.listing_count_stable", WARN, change <= MAX_LISTING_CHANGE, round(change, 3),
            f"<= {MAX_LISTING_CHANGE:.0%}", f"{n} vs {prev[1]} in run {prev[0]}",
        ))

    fetched = scalar(con, "SELECT max(fetched_at) FROM google_listings WHERE run_id = ?", run_id)
    age = (today - fetched.date()).days
    results.append(Result(
        "google.fresh", WARN, age <= MAX_GOOGLE_AGE_DAYS, age, f"<= {MAX_GOOGLE_AGE_DAYS} days",
        f"fetched {fetched:%Y-%m-%d}; source {scalar(con, 'SELECT any_value(source) FROM google_listings WHERE run_id = ?', run_id)}",
    ))

    missing = con.execute(
        "SELECT list(DISTINCT category) FROM google_listings WHERE run_id = ?", [run_id]
    ).fetchone()[0]
    absent = sorted(set(CATEGORIES) - set(missing or []))
    results.append(Result(
        "google.all_categories", ERROR, not absent, len(absent), "0 missing",
        f"missing: {', '.join(absent)}" if absent else "all categories present",
    ))

    bad = scalar(
        con,
        """SELECT count(*) FROM google_listings WHERE run_id = ?
           AND (review_count < 0 OR rating NOT BETWEEN 1 AND 5 OR lat IS NULL OR lon IS NULL)""",
        run_id,
    )
    results.append(Result("google.valid_values", WARN, bad == 0, bad, "0", "negative reviews, rating outside 1-5 or missing coordinates"))
    return results


def sodra_checks(con, today: date) -> list[Result]:
    n = scalar(con, "SELECT count(*) FROM sodra_monthly")
    results = [Result("sodra.rows_present", ERROR, n > 0, n, "> 0", f"{n:,} rows")]
    if not n:
        return results

    latest = scalar(con, "SELECT max(month) FROM sodra_monthly")
    lag = months_between(today, latest)
    results.append(Result(
        "sodra.fresh", WARN, lag <= MAX_SODRA_LAG_MONTHS, lag, f"<= {MAX_SODRA_LAG_MONTHS} months",
        f"latest month {latest:%Y-%m}",
    ))

    months = con.execute("SELECT month, count(*) FROM sodra_monthly GROUP BY 1 ORDER BY 1").fetchall()
    expected = months_between(latest, months[0][0]) + 1
    results.append(Result(
        "sodra.months_continuous", ERROR, len(months) == expected, len(months), f"{expected} months",
        f"{months[0][0]:%Y-%m} to {latest:%Y-%m}",
    ))
    results.append(Result(
        "sodra.history_length", WARN, len(months) >= MIN_SODRA_MONTHS, len(months), f">= {MIN_SODRA_MONTHS}",
        "needed for 12-month averages and seasonality",
    ))
    counts = [c for _, c in months]
    ratio = min(counts) / max(counts)
    results.append(Result(
        "sodra.month_volume_stable", WARN, ratio >= MIN_MONTH_ROWS_RATIO, round(ratio, 3),
        f">= {MIN_MONTH_ROWS_RATIO}", f"rows per month {min(counts):,} to {max(counts):,}",
    ))

    unparsed = scalar(con, "SELECT count(*) FROM sodra_monthly WHERE insured IS NULL")
    results.append(Result(
        "sodra.insured_parsed", WARN, unparsed / n < 0.01, round(unparsed / n, 4), "< 1%", f"{unparsed:,} rows without insured count",
    ))
    bad_codes = scalar(con, "SELECT count(*) FROM sodra_monthly WHERE NOT regexp_full_match(company_code, '\\d{9}')")
    results.append(Result("sodra.company_codes_valid", ERROR, bad_codes == 0, bad_codes, "0", "company codes must be 9 digits"))
    return results


def registry_checks(con, today: date) -> list[Result]:
    n = scalar(con, "SELECT count(*) FROM registry_entities")
    results = [Result("registry.complete", ERROR, n >= MIN_REGISTRY_ROWS, n, f">= {MIN_REGISTRY_ROWS:,}", f"{n:,} legal entities")]
    if not n:
        return results
    loaded = scalar(con, "SELECT max(loaded_at) FROM registry_entities")
    age = (today - loaded.date()).days
    results.append(Result(
        "registry.fresh", WARN, age <= MAX_REGISTRY_AGE_DAYS, age, f"<= {MAX_REGISTRY_AGE_DAYS} days", f"loaded {loaded:%Y-%m-%d}",
    ))
    bad_codes = scalar(con, "SELECT count(*) FROM registry_entities WHERE NOT regexp_full_match(company_code, '\\d{9}')")
    results.append(Result("registry.company_codes_valid", ERROR, bad_codes == 0, bad_codes, "0", "company codes must be 9 digits"))
    unresolved = scalar(con, "SELECT count(*) FROM registry_entities WHERE legal_form IS NULL OR status IS NULL")
    results.append(Result(
        "registry.classifiers_resolved", WARN, unresolved / n < 0.01, round(unresolved / n, 4), "< 1%",
        f"{unresolved:,} entities without legal form or status",
    ))
    return results


def match_checks(con, run_id: str) -> list[Result]:
    results = []
    unmatched_rows = scalar(
        con,
        """SELECT count(*) FROM google_listings g ANTI JOIN matches m USING (run_id, place_id, category)
           WHERE g.run_id = ?""",
        run_id,
    )
    results.append(Result(
        "match.every_listing_decided", ERROR, unmatched_rows == 0, unmatched_rows, "0", "listings without a match decision",
    ))
    orphans = scalar(
        con,
        """SELECT count(*) FROM matches m ANTI JOIN registry_entities r USING (company_code)
           WHERE m.run_id = ? AND m.accepted""",
        run_id,
    )
    results.append(Result("match.accepted_in_registry", ERROR, orphans == 0, orphans, "0", "accepted matches to unknown company codes"))
    for category, rate, total in con.execute(
        "SELECT category, avg(accepted::INT), count(*) FROM matches WHERE run_id = ? GROUP BY 1 ORDER BY 1", [run_id]
    ).fetchall():
        results.append(Result(
            f"match.rate.{category}", WARN, rate >= MIN_MATCH_RATE, round(rate, 3), f">= {MIN_MATCH_RATE:.0%}",
            f"{round(rate * total)} of {total} listings matched",
        ))
    return results


def lead_checks(con, run_id: str) -> list[Result]:
    results = []
    missing = scalar(
        con,
        """SELECT count(*) FROM (SELECT DISTINCT company_code, category FROM matches m
                                 JOIN google_listings g USING (run_id, place_id, category)
                                 WHERE m.run_id = ? AND m.accepted AND g.business_status = 'OPERATIONAL') a
           ANTI JOIN (SELECT company_code, category FROM leads WHERE run_id = ?) l USING (company_code, category)""",
        run_id, run_id,
    )
    results.append(Result("leads.every_match_scored", ERROR, missing == 0, missing, "0", "accepted companies without a lead row"))

    flagged, eligible = con.execute(
        """SELECT count(*) FILTER (WHERE flagged), count(*) FILTER (WHERE exclusion_reason IS NULL)
           FROM leads WHERE run_id = ?""",
        [run_id],
    ).fetchone()
    share = flagged / eligible if eligible else 0.0
    results.append(Result(
        "leads.flagged_share_plausible", WARN, share <= MAX_FLAGGED_SHARE, round(share, 3), f"<= {MAX_FLAGGED_SHARE:.0%}",
        f"{flagged} flagged of {eligible} eligible; a jump suggests a bug rather than a wave of evasion",
    ))
    unexplained = scalar(
        con, "SELECT count(*) FROM leads WHERE run_id = ? AND flagged AND len(reasons) < 2", run_id
    )
    results.append(Result("leads.flagged_explained", ERROR, unexplained == 0, unexplained, "0", "flagged leads without reasons"))
    out_of_range = scalar(
        con,
        "SELECT count(*) FROM leads WHERE run_id = ? AND (score NOT BETWEEN 0 AND 100 OR match_confidence < ?)",
        run_id, MATCH_THRESHOLD,
    )
    results.append(Result("leads.values_in_range", ERROR, out_of_range == 0, out_of_range, "0", "score outside 0-100 or unaccepted match scored"))
    return results


def evaluate(con: duckdb.DuckDBPyConnection, run_id: str, today: date) -> list[Result]:
    return [
        *google_checks(con, run_id, today),
        *sodra_checks(con, today),
        *registry_checks(con, today),
        *match_checks(con, run_id),
        *lead_checks(con, run_id),
    ]


def run_checks(run_id: str) -> bool:
    checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    with db.connect() as con:
        db.init_schema(con)
        results = evaluate(con, run_id, checked_at.date())
        con.execute("DELETE FROM dq_checks WHERE run_id = ?", [run_id])
        con.executemany(
            "INSERT INTO dq_checks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [[run_id, r.check, r.severity, r.passed, r.value, r.threshold, r.details, checked_at] for r in results],
        )
    for r in results:
        if not r.passed:
            (log.error if r.severity == ERROR else log.warning)("DQ %s failed: %s (%s)", r.check, r.value, r.details)
    errors = sum(not r.passed and r.severity == ERROR for r in results)
    warnings = sum(not r.passed and r.severity == WARN for r in results)
    log.info("DQ: %d checks, %d errors, %d warnings", len(results), errors, warnings)
    return errors == 0
