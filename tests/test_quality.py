from datetime import date, datetime

import duckdb
import pytest

from shadow import db, quality

TODAY = date(2026, 10, 27)
RUN = "r2"


@pytest.fixture
def con():
    c = duckdb.connect()
    db.init_schema(c)
    yield c
    c.close()


def add_listing(con, run_id, place_id, category="coffee_shop", reviews=100, fetched=datetime(2026, 10, 25)):
    con.execute(
        """INSERT INTO google_listings (run_id, place_id, category, name, lat, lon, rating, review_count,
           business_status, source, fetched_at, raw)
           VALUES (?, ?, ?, 'x', 54.68, 25.28, 4.5, ?, 'OPERATIONAL', 'live', ?, '{}')""",
        [run_id, place_id, category, reviews, fetched],
    )


def add_match(con, run_id, place_id, company_code, category="coffee_shop", accepted=True, confidence=0.98):
    con.execute(
        "INSERT INTO matches (run_id, place_id, category, company_code, confidence, accepted, method) VALUES (?, ?, ?, ?, ?, ?, 'name')",
        [run_id, place_id, category, company_code, confidence, accepted],
    )


def add_lead(con, run_id, company_code, category="coffee_shop", flagged=False, score=10.0, reasons=("a", "b")):
    con.execute(
        """INSERT INTO leads (run_id, company_code, category, flagged, score, match_confidence, exclusion_reason, reasons)
           VALUES (?, ?, ?, ?, ?, 0.98, NULL, ?)""",
        [run_id, company_code, category, flagged, score, list(reasons)],
    )


@pytest.fixture
def healthy(con):
    loaded = datetime(2026, 10, 26)
    con.execute(
        "INSERT INTO registry_entities SELECT lpad((100000000 + i)::VARCHAR, 9, '0'), 'n', 'UAB', 'ok', NULL, NULL, NULL, ? "
        "FROM range(?) t(i)",
        [loaded, quality.MIN_REGISTRY_ROWS],
    )
    con.execute(
        """INSERT INTO sodra_monthly (month, company_code, insured, source_file, loaded_at)
           SELECT (DATE '2025-09-01' + INTERVAL (m) MONTH)::DATE, lpad((100000000 + c)::VARCHAR, 9, '0'), 3, 'f', ?
           FROM range(13) a(m), range(5) b(c)""",
        [loaded],
    )
    for i, cat in enumerate(["coffee_shop", "car_repair", "florist"]):
        code = f"10000000{i}"
        add_listing(con, RUN, f"p{i}", cat)
        add_match(con, RUN, f"p{i}", code, cat)
        add_lead(con, RUN, code, cat)
    return con


def failed(results):
    return {r.check: r.severity for r in results if not r.passed}


def test_healthy_run_passes(healthy):
    assert failed(quality.evaluate(healthy, RUN, TODAY)) == {}


def test_empty_run_is_error(con):
    results = quality.evaluate(con, "missing", TODAY)
    assert failed(results)["google.listings_present"] == quality.ERROR


def test_listing_without_match_decision_is_error(healthy):
    add_listing(healthy, RUN, "p_extra")
    assert failed(quality.evaluate(healthy, RUN, TODAY))["match.every_listing_decided"] == quality.ERROR


def test_accepted_match_to_unknown_company_is_error(healthy):
    add_listing(healthy, RUN, "p_orphan")
    add_match(healthy, RUN, "p_orphan", "399999999")
    add_lead(healthy, RUN, "399999999")
    assert failed(quality.evaluate(healthy, RUN, TODAY))["match.accepted_in_registry"] == quality.ERROR


def test_listing_count_jump_is_warning(healthy):
    for i in range(10):
        add_listing(healthy, "r1", f"old{i}")
    assert failed(quality.evaluate(healthy, RUN, TODAY))["google.listing_count_stable"] == quality.WARN


def test_stale_sources_are_warnings(healthy):
    late = quality.evaluate(healthy, RUN, date(2027, 6, 1))
    f = failed(late)
    assert f["sodra.fresh"] == quality.WARN
    assert f["google.fresh"] == quality.WARN
    assert f["registry.fresh"] == quality.WARN


def test_sodra_month_gap_is_error(healthy):
    healthy.execute("DELETE FROM sodra_monthly WHERE month = DATE '2026-03-01'")
    assert failed(quality.evaluate(healthy, RUN, TODAY))["sodra.months_continuous"] == quality.ERROR


def test_implausible_flagged_share_is_warning(healthy):
    healthy.execute("UPDATE leads SET flagged = TRUE WHERE run_id = ?", [RUN])
    assert failed(quality.evaluate(healthy, RUN, TODAY))["leads.flagged_share_plausible"] == quality.WARN
