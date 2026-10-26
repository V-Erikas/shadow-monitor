import pytest

from shadow import match
from shadow.collect import websites
from shadow.match import Candidates, Company

COMPANIES = [
    Company("305636261", 'UAB "Kačių kavinė"', "56.30.00"),
    Company("300651366", "UAB MELGA", "95.31.00"),
    Company("111111111", "MB Pupa", "56.30.00"),
    Company("122222222", "UAB Pupa", "47.76.00"),
    Company("133333333", "UAB Rožių namai", None),
    Company("144444444", "UAB Rožių nameliai", "47.76.00"),
]
CANDS = Candidates(COMPANIES)


def listing(name, category="coffee_shop", website=None):
    return {"run_id": "r1", "place_id": "p1", "category": category, "name": name, "website": website}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('UAB "Kačių kavinė"', "kaciu"),
        ("Mažoji bendrija Pupa", "pupa"),
        ("Gėlių Fėja", "feja"),
        (None, ""),
    ],
)
def test_normalize(raw, expected):
    assert match.normalize(raw) == expected


def test_google_core_name_drops_seo_suffixes():
    assert match.google_core_name("MELGA Autoservisas, padangos Vilniuje | Vilkpėdė") == "melga"
    assert match.google_core_name("Autvadita - Specializuotas servisas. BMW") == "autvadita"


def test_exact_name_with_matching_activity_code():
    r = match.match_by_name("kaciu", "coffee_shop", CANDS)
    assert r["name_candidate"] == "305636261"
    assert r["name_confidence"] == match.NAME_EXACT_NACE


def test_same_name_resolved_by_activity_code():
    r = match.match_by_name("pupa", "florist", CANDS)
    assert r["name_candidate"] == "122222222"
    assert r["name_confidence"] == match.NAME_EXACT_NACE


def test_activity_code_disagreement_is_below_threshold():
    r = match.match_by_name("melga", "coffee_shop", CANDS)
    assert r["name_confidence"] == match.NAME_NACE_DISAGREES < match.MATCH_THRESHOLD


def test_close_runner_up_is_ambiguous():
    r = match.match_by_name("roziu namai", "florist", CANDS)
    assert r["name_reason"] in {"ambiguous_name", "name_match_no_sodra_record"}
    assert r["name_confidence"] < match.MATCH_THRESHOLD


def test_short_generic_name_unmatched():
    assert match.match_by_name("", "coffee_shop", CANDS)["name_reason"] == "generic_or_short_name"


def test_extract_codes_anchored_and_ignores_vat():
    html = "<footer>UAB Pupa, įmonės kodas: 111111111, PVM kodas LT100001234567, tel. 861234567</footer>"
    codes = websites.extract_codes(html, "https://pupa.lt/")
    assert codes == [{"code": "111111111", "anchored": True, "page": "https://pupa.lt/"}]


def test_extract_codes_loose():
    codes = websites.extract_codes("<p>Rekvizitai: 305636261</p>", "u")
    assert codes == [{"code": "305636261", "anchored": False, "page": "u"}]


def test_subpage_links_same_host_only():
    html = (
        '<a href="/kontaktai">Kontaktai</a><a href="https://facebook.com/contact">FB</a>'
        '<a href="/privatumo-politika">Privatumo politika</a><a href="/meniu">Meniu</a>'
    )
    assert websites.subpage_links(html, "https://pupa.lt/") == [
        "https://pupa.lt/kontaktai",
        "https://pupa.lt/privatumo-politika",
    ]


def test_decide_website_code_beats_name():
    scan = {"codes": [{"code": "111111111", "anchored": True, "page": "https://pupa.lt/"}]}
    r = match.decide(listing("Pupa"), scan, CANDS)
    assert r["company_code"] == "111111111"
    assert r["method"] == "website_code+name"
    assert r["accepted"]


def test_decide_unknown_website_code_falls_back_to_name():
    scan = {"codes": [{"code": "199999999", "anchored": True, "page": "u"}]}
    r = match.decide(listing("Kačių Kavinė"), scan, CANDS)
    assert r["method"] == "name"
    assert r["company_code"] == "305636261"


def test_decide_unmatched_keeps_reason():
    r = match.decide(listing("Kavinė"), None, CANDS)
    assert r["company_code"] is None
    assert not r["accepted"]
    assert r["reason"] == "generic_or_short_name"


def test_decide_website_code_from_other_industry_not_accepted():
    # e.g. the web developer's code in the footer
    scan = {"codes": [{"code": "300651366", "anchored": True, "page": "u"}]}
    r = match.decide(listing("Gėlių Fėja", "florist"), scan, CANDS)
    assert r["method"] == "website_code"
    assert not r["accepted"]


def test_decide_website_code_from_related_industry_accepted():
    # e.g. coffee chain operated by a convenience-store company
    cands = Candidates([*COMPANIES, Company("155555555", "UAB Reitan", "47.11.00")])
    scan = {"codes": [{"code": "155555555", "anchored": True, "page": "u"}]}
    r = match.decide(listing("Caffeine"), scan, cands)
    assert r["confidence"] == match.WEBSITE_ANCHORED_RELATED
    assert r["accepted"]


def test_decide_website_code_without_sodra_record_accepted():
    scan = {"codes": [{"code": "133333333", "anchored": True, "page": "u"}]}
    r = match.decide(listing("Rožės", "florist"), scan, CANDS)
    assert r["confidence"] == match.WEBSITE_ANCHORED_NO_SODRA
    assert r["accepted"]


def test_decide_conflict_between_website_and_name():
    scan = {"codes": [{"code": "144444444", "anchored": True, "page": "u"}]}
    r = match.decide(listing("Kačių Kavinė"), scan, CANDS)
    assert r["method"] == "conflict"
    assert not r["accepted"]


def test_clean_url():
    assert websites.clean_url("HTTPS://Pupa.LT?utm_source=google") == "https://pupa.lt/"
