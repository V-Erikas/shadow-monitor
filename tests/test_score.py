from datetime import date

import pandas as pd
import pytest

from shadow import score

PEER_WAGE = {"coffee_shop": 1500.0, "car_repair": 1600.0, "florist": 1200.0}


def company(code, reviews, staff, *, category="coffee_shop", hours=60.0, wage=1500.0, **overrides):
    row = {
        "company_code": code,
        "category": category,
        "n_listings": 1,
        "place_ids": [f"p{code}"],
        "listing_names": [f"Cafe {code}"],
        "total_reviews": reviews,
        "avg_rating": 4.5,
        "weekly_open_hours": hours,
        "match_confidence": 0.98,
        "match_methods": ["website_code"],
        "review_delta": None,
        "company_name": f"UAB {code}",
        "legal_form": "Uždaroji akcinė bendrovė",
        "legal_status": score.ACTIVE_STATUS,
        "registered_on": date(2018, 1, 1),
        "in_sodra": True,
        "insured_latest": staff,
        "insured_max_3m": staff,
        "insured_avg_12m": float(staff),
        "insured_12m_ago": staff,
        "avg_wage": wage if staff >= 3 else None,
        "nace_code": "56.30.00",
        "nace_name": "Gėrimų teikimas",
        "sodra_month": date(2026, 9, 1),
    }
    return {**row, **overrides}


def peers():
    # Honest peers: staff grows with reviews
    return [company(f"1{i:08d}", reviews, staff) for i, (reviews, staff) in enumerate(
        [(200, 3), (300, 4), (400, 5), (600, 6), (800, 8), (1000, 9), (1500, 12), (2000, 14),
         (2500, 16), (3000, 18), (250, 3), (700, 7)]
    )]


def frame(*extra):
    return pd.DataFrame([*peers(), *extra])


def test_honest_peers_not_flagged():
    df = score.score_frame(frame(), PEER_WAGE)
    assert not df["flagged"].any()


def test_busy_cafe_with_one_employee_flagged_on_staffing_gap():
    # The task's example: 2,000 reviews, one employee
    shadow = company("300000001", 2000, 1, hours=84)
    lead = score.score_frame(frame(shadow), PEER_WAGE).set_index("company_code").loc["300000001"]
    assert lead["flagged"]
    assert lead["tier"] == "staffing gap only"
    assert any("Staffing gap" in r for r in lead["reasons"])


def test_multiple_signals_rank_above_single_signal():
    single = company("300000001", 2000, 1, hours=84)
    multi = company("300000009", 2000, 1, hours=126)
    df = score.score_frame(frame(single, multi), PEER_WAGE).set_index("company_code")
    assert df.loc["300000009", "tier"] == "multiple signals"
    assert df.loc["300000009", "hours_score"] >= score.STRONG_SIGNAL
    assert any("Opening hours" in r for r in df.loc["300000009", "reasons"])
    assert df.loc["300000009", "rank"] < df.loc["300000001", "rank"]


def test_moderate_single_signal_is_not_enough():
    shadow = company("300000002", 2000, 5, hours=30)
    lead = score.score_frame(frame(shadow), PEER_WAGE).set_index("company_code").loc["300000002"]
    assert score.STRONG_SIGNAL <= lead["staffing_score"] < score.STAFFING_ALONE
    assert lead["signals_on"] == 1
    assert not lead["flagged"]
    assert lead["tier"] == "watchlist"
    assert pd.isna(lead["rank"])


def test_quiet_corner_shop_not_flagged():
    quiet = company("300000003", 40, 1, hours=50)
    lead = score.score_frame(frame(quiet), PEER_WAGE).set_index("company_code").loc["300000003"]
    assert not lead["flagged"]
    assert lead["exclusion_reason"].startswith("below review threshold")


def test_no_sodra_record_counts_as_zero_staff():
    ghost = company(
        "300000004", 1500, 0, hours=70, in_sodra=False, insured_latest=None, insured_max_3m=None,
        insured_avg_12m=None, insured_12m_ago=None, avg_wage=None,
    )
    lead = score.score_frame(frame(ghost), PEER_WAGE).set_index("company_code").loc["300000004"]
    assert lead["declared_staff"] == 0
    assert lead["flagged"]
    assert any("No Sodra record" in r for r in lead["reasons"])


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"legal_status": "Bankrutuojantis"}, "legal status"),
        ({"registered_on": date(2026, 3, 1)}, "younger than"),
    ],
)
def test_exclusions(overrides, reason):
    shadow = company("300000005", 2000, 1, hours=84, **overrides)
    lead = score.score_frame(frame(shadow), PEER_WAGE).set_index("company_code").loc["300000005"]
    assert not lead["flagged"]
    assert reason in lead["exclusion_reason"]


def test_low_wage_signal_and_caveats():
    mb = company("300000006", 2000, 4, hours=84, wage=700, legal_form="Mažoji bendrija", match_methods=["name"])
    lead = score.score_frame(frame(mb), PEER_WAGE).set_index("company_code").loc["300000006"]
    assert lead["wage_score"] >= score.STRONG_SIGNAL
    assert any("Low wage" in r for r in lead["reasons"])
    assert any("Mažoji bendrija" in c for c in lead["caveats"])
    assert any("name only" in c for c in lead["caveats"])


def test_lower_match_confidence_lowers_score():
    a = company("300000007", 2000, 1, hours=84, match_confidence=0.99)
    b = company("300000008", 2000, 1, hours=84, match_confidence=0.82)
    df = score.score_frame(frame(a, b), PEER_WAGE).set_index("company_code")
    assert df.loc["300000007", "score"] > df.loc["300000008", "score"]
