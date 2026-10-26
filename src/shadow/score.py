"""Each matched company gets three independent signals (0-1). An eligible company is flagged when
at least MIN_SIGNALS are strong, or when the staffing gap alone is extreme; the tier says which.
"""
import logging
import math

import numpy as np
import pandas as pd
from sklearn.linear_model import HuberRegressor

from shadow import db
from shadow.config import CATEGORIES, OUTPUT_DIR

log = logging.getLogger(__name__)

STRONG_SIGNAL = 0.5
MIN_SIGNALS = 2
STAFFING_ALONE = 0.9
MIN_COMPANY_AGE_MONTHS = 12
FULL_TIME_HOURS = 40
OWNER_ALLOWANCE = 1  # owner / MB member may work without being an insured employee
ACTIVE_STATUS = "Teisinis statusas neįregistruotas"  # i.e. no bankruptcy / liquidation proceedings
WEIGHTS = {"staffing": 0.5, "hours": 0.3, "wage": 0.2}
MIN_MODEL_ROWS = 10

FEATURES_SQL = """
WITH listings AS (
    SELECT m.company_code, m.category, g.place_id, g.name, g.review_count, g.rating,
           g.weekly_open_hours, m.confidence, m.method, p.review_count AS prev_review_count
    FROM matches m
    JOIN google_listings g USING (run_id, place_id, category)
    LEFT JOIN google_listings p
           ON p.place_id = g.place_id AND p.category = g.category
          AND p.run_id = (SELECT max(run_id) FROM google_listings WHERE run_id < $run_id)
    WHERE m.run_id = $run_id AND m.accepted AND g.business_status = 'OPERATIONAL'
),
agg AS (
    SELECT company_code, category,
           count(*) AS n_listings,
           list(place_id ORDER BY review_count DESC) AS place_ids,
           list(name ORDER BY review_count DESC) AS listing_names,
           sum(review_count)::INTEGER AS total_reviews,
           sum(review_count * rating) / nullif(sum(review_count), 0) AS avg_rating,
           sum(weekly_open_hours) AS weekly_open_hours,
           min(confidence) AS match_confidence,
           list(DISTINCT method) AS match_methods,
           sum(review_count - prev_review_count)::INTEGER AS review_delta
    FROM listings GROUP BY ALL
),
lm AS (SELECT max(month) AS m FROM sodra_monthly),
sodra AS (
    SELECT company_code,
           TRUE AS in_sodra,
           max(insured) FILTER (WHERE month = lm.m) AS insured_latest,
           max(insured) FILTER (WHERE month > lm.m - INTERVAL 3 MONTH) AS insured_max_3m,
           avg(insured) FILTER (WHERE month > lm.m - INTERVAL 12 MONTH) AS insured_avg_12m,
           max(insured) FILTER (WHERE month = lm.m - INTERVAL 12 MONTH) AS insured_12m_ago,
           max(avg_wage) FILTER (WHERE month = lm.m) AS avg_wage,
           arg_max(nace_code, month) AS nace_code,
           arg_max(nace_name, month) AS nace_name
    FROM sodra_monthly, lm GROUP BY company_code
)
SELECT a.*, r.name AS company_name, r.legal_form, r.status AS legal_status, r.registered_on,
       s.* EXCLUDE (company_code, in_sodra), coalesce(s.in_sodra, FALSE) AS in_sodra,
       (SELECT m FROM lm) AS sodra_month
FROM agg a
JOIN registry_entities r USING (company_code)
LEFT JOIN sodra s USING (company_code)
"""

PEER_WAGE_SQL = """
SELECT avg_wage FROM sodra_monthly
WHERE month = (SELECT max(month) FROM sodra_monthly)
  AND municipality = 'Vilniaus m. sav.' AND avg_wage IS NOT NULL AND starts_with(nace_code, ?)
"""


def clip01(x: float) -> float:
    return float(min(max(x, 0.0), 1.0))


def peer_wages(con) -> dict[str, float]:
    medians = {}
    for key, cat in CATEGORIES.items():
        wages = [w for code in cat.nace_codes for (w,) in con.execute(PEER_WAGE_SQL, [code]).fetchall()]
        medians[key] = float(np.median(wages)) if wages else math.nan
    return medians


def fit_expected_staff(df: pd.DataFrame) -> pd.Series:
    """Robust log-log fit of declared staff on reviews, shared slope with per-category intercepts."""
    cats = sorted(CATEGORIES)

    def design(frame: pd.DataFrame) -> np.ndarray:
        dummies = np.column_stack([(frame["category"] == c).astype(float) for c in cats[1:]])
        return np.column_stack([np.log1p(frame["total_reviews"]), dummies])

    train = df[df["in_sodra"] & (df["insured_avg_12m"] >= 1)]
    if len(train) < MIN_MODEL_ROWS:
        log.warning("Only %d companies to fit staffing model; staffing signal disabled", len(train))
        return pd.Series(math.nan, index=df.index)
    model = HuberRegressor().fit(design(train), np.log1p(train["insured_avg_12m"]))
    log.info("Staffing model: %d companies, reviews elasticity %.2f", len(train), model.coef_[0])
    return pd.Series(np.expm1(model.predict(design(df))), index=df.index)


def compute_signals(df: pd.DataFrame, peer_wage: dict[str, float]) -> pd.DataFrame:
    df = df.copy()
    # Benefit of the doubt: highest headcount in the last 3 months, not just the latest month
    df["declared_staff"] = df["insured_max_3m"].fillna(0).astype(int)
    df["expected_staff"] = fit_expected_staff(df)
    df["staffing_score"] = [
        clip01((math.log1p(e) - math.log1p(d)) / math.log(4)) if not math.isnan(e) else 0.0
        for e, d in zip(df["expected_staff"], df["declared_staff"])
    ]
    df["min_staff_for_hours"] = df["weekly_open_hours"] / FULL_TIME_HOURS
    df["hours_score"] = [
        clip01((h - (d + OWNER_ALLOWANCE)) / 2) if pd.notna(h) else 0.0
        for h, d in zip(df["min_staff_for_hours"], df["declared_staff"])
    ]
    df["peer_median_wage"] = df["category"].map(peer_wage)
    df["wage_ratio"] = df["avg_wage"] / df["peer_median_wage"]
    df["wage_score"] = [clip01((0.8 - r) / 0.4) if pd.notna(r) else 0.0 for r in df["wage_ratio"]]
    df["signals_on"] = (df[["staffing_score", "hours_score", "wage_score"]] >= STRONG_SIGNAL).sum(axis=1)
    weighted = sum(df[f"{k}_score"] * w for k, w in WEIGHTS.items())
    df["score"] = (100 * weighted * df["match_confidence"]).round(1)
    return df


def exclusion_reason(row: pd.Series) -> str | None:
    cat = CATEGORIES[row["category"]]
    if row["total_reviews"] < cat.min_reviews:
        return f"below review threshold ({row['total_reviews']} < {cat.min_reviews})"
    if row["legal_status"] != ACTIVE_STATUS:
        return f"legal status: {row['legal_status']}"
    if pd.notna(row["registered_on"]):
        age_months = (row["sodra_month"].year - row["registered_on"].year) * 12 + (
            row["sodra_month"].month - row["registered_on"].month
        )
        if age_months < MIN_COMPANY_AGE_MONTHS:
            return f"company younger than {MIN_COMPANY_AGE_MONTHS} months"
    return None


def explain(row: pd.Series) -> tuple[list[str], list[str]]:
    label = CATEGORIES[row["category"]].label.lower()
    declared, month = row["declared_staff"], row["sodra_month"].strftime("%Y-%m")
    reasons = [
        f"{row['total_reviews']:,} Google reviews across {row['n_listings']} listing(s)"
        + (f", +{int(row['review_delta'])} since previous run" if pd.notna(row["review_delta"]) else "")
    ]
    if not row["in_sodra"]:
        reasons.append("No Sodra record: the company reports no insured persons")
    elif pd.isna(row["insured_avg_12m"]):
        reasons.append(f"No insured persons reported to Sodra in the 12 months to {month}")
    else:
        reasons.append(
            f"Declares {declared} insured (max of last 3 months to {month}); "
            f"12-month average {row['insured_avg_12m']:.1f}"
        )
    if row["staffing_score"] > 0:
        reasons.append(
            f"Staffing gap: {label} with similar review volume declare ~{row['expected_staff']:.0f} insured"
        )
    if row["hours_score"] > 0:
        reasons.append(
            f"Opening hours: {row['weekly_open_hours']:.0f} h/week need at least "
            f"{row['min_staff_for_hours']:.1f} full-time staff to keep one person on site "
            f"(declared {declared} + {OWNER_ALLOWANCE} owner allowance)"
        )
    if row["wage_score"] > 0:
        reasons.append(
            f"Low wage: average €{row['avg_wage']:,.0f} is {row['wage_ratio']:.0%} of the Vilnius {label} "
            f"median (€{row['peer_median_wage']:,.0f})"
        )

    caveats = []
    if row["legal_form"] in ("Mažoji bendrija", "Individuali įmonė"):
        caveats.append(f"{row['legal_form']}: owners may work without being insured employees")
    if row["match_methods"] == ["name"]:
        caveats.append("Matched by name only (no company code on website)")
    if row["nace_code"] and not str(row["nace_code"]).startswith(CATEGORIES[row["category"]].nace_codes):
        caveats.append(f"Registered activity is {row['nace_code']} {row['nace_name']}: may have other revenue streams")
    if row["in_sodra"] and pd.isna(row["avg_wage"]):
        caveats.append("Wage hidden by Sodra (< 3 insured)")
    if row["category"] == "florist":
        caveats.append("Flower shops are seasonal; check peak months before acting")
    return reasons, caveats


def score_frame(features: pd.DataFrame, peer_wage: dict[str, float]) -> pd.DataFrame:
    features = features.copy()
    # DuckDB returns LIST columns as numpy arrays
    for col in ("place_ids", "listing_names", "match_methods"):
        features[col] = features[col].map(list)
    df = compute_signals(features, peer_wage)
    df["exclusion_reason"] = df.apply(exclusion_reason, axis=1)
    eligible = df["exclusion_reason"].isna()
    multiple = df["signals_on"] >= MIN_SIGNALS
    staffing_only = ~multiple & (df["staffing_score"] >= STAFFING_ALONE)
    df["flagged"] = eligible & (multiple | staffing_only)
    df["tier"] = None
    df.loc[df["flagged"] & multiple, "tier"] = "multiple signals"
    df.loc[df["flagged"] & staffing_only, "tier"] = "staffing gap only"
    # One strong signal: shown to the analyst for context, not a lead
    df.loc[eligible & ~df["flagged"] & (df["signals_on"] >= 1), "tier"] = "watchlist"
    explained = df.apply(explain, axis=1)
    df["reasons"] = [r for r, _ in explained]
    df["caveats"] = [c for _, c in explained]
    df["rank"] = df["score"].where(df["flagged"]).rank(ascending=False, method="first").astype("Int64")
    return df


LEAD_COLUMNS = [
    "run_id", "company_code", "category", "company_name", "legal_form", "nace_code", "n_listings",
    "place_ids", "listing_names", "total_reviews", "avg_rating", "review_delta", "weekly_open_hours",
    "declared_staff", "insured_latest", "insured_avg_12m", "insured_12m_ago", "sodra_month", "in_sodra",
    "avg_wage", "peer_median_wage", "wage_ratio", "expected_staff", "min_staff_for_hours",
    "staffing_score", "hours_score", "wage_score", "signals_on", "match_confidence", "match_methods",
    "score", "flagged", "tier", "rank", "exclusion_reason", "reasons", "caveats",
]


def score_leads(run_id: str) -> int:
    with db.connect() as con:
        db.init_schema(con)
        features = con.execute(FEATURES_SQL, {"run_id": run_id}).df()
        if features.empty:
            log.warning("No accepted matches for run %s", run_id)
            return 0
        df = score_frame(features, peer_wages(con))
        df["run_id"] = run_id
        leads = df[LEAD_COLUMNS]
        con.execute("DELETE FROM leads WHERE run_id = ?", [run_id])
        con.register("leads_df", leads)
        con.execute(f"INSERT INTO leads ({', '.join(LEAD_COLUMNS)}) SELECT {', '.join(LEAD_COLUMNS)} FROM leads_df")
        con.unregister("leads_df")

    export(leads[leads["tier"].notna()].sort_values(["flagged", "score"], ascending=False), run_id)
    flagged = int(leads["flagged"].sum())
    log.info("Scored %d companies, flagged %d", len(leads), flagged)
    return flagged


def export(leads: pd.DataFrame, run_id: str) -> None:
    out = leads.copy()
    for col in ("place_ids", "listing_names", "match_methods", "reasons", "caveats"):
        out[col] = out[col].map(lambda v: " | ".join(map(str, v)))
    path = OUTPUT_DIR / f"leads_{run_id}.csv"
    out.to_csv(path, index=False)
    log.info("Lead list written to %s", path)
