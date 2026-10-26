"""Link Google listings to legal entities (company code).

Layered: company code published on the business website, then fuzzy name match checked
against the company's Sodra activity code. Anything below MATCH_THRESHOLD stays unmatched.
"""
import logging
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass

from rapidfuzz import fuzz, process

from shadow import db
from shadow.collect.websites import clean_url, scan_websites
from shadow.config import CATEGORIES, MATCH_THRESHOLD

log = logging.getLogger(__name__)

STOPWORDS = {
    # legal forms
    "uab", "mb", "ii", "ab", "vsi", "uzdaroji", "akcine", "bendrove", "mazoji", "bendrija",
    "individuali", "imone", "viesoji", "istaiga",
    # generic category / location words used in Google names
    "kavine", "kava", "kavos", "coffee", "cafe", "caffe", "kaffee", "roasters", "baras", "bar",
    "autoservisas", "autoservis", "servisas", "service", "auto", "padangos", "padangu", "tyres",
    "geles", "geliu", "gele", "flowers", "flower", "floristika", "shop", "parduotuve", "salonas",
    "studija", "vilnius", "vilniuje", "lt", "ir", "and", "the",
}
GOOGLE_NAME_SEPARATORS = re.compile(r"\s+[|\-–—]\s+|[|,]")

# Confidence levels
WEBSITE_ANCHORED_NACE = 0.98
WEBSITE_ANCHORED_NO_SODRA = 0.9
WEBSITE_ANCHORED_RELATED = 0.85
# Often the web developer's or a partner's code (e.g. IT company, Regitra) rather than the business
WEBSITE_ANCHORED_OTHER_INDUSTRY = 0.7
WEBSITE_AND_NAME = 0.99
WEBSITE_LOOSE = 0.75
WEBSITE_LOOSE_NACE = 0.85
CONFLICT = 0.5
NAME_EXACT_NACE = 0.9
NAME_HIGH_NACE = 0.82
NAME_NO_NACE = 0.6
NAME_NACE_DISAGREES = 0.5
NAME_MIN_SCORE = 92
NAME_MIN_MARGIN = 5


def normalize(name: str | None) -> str:
    if not name:
        return ""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    tokens = re.sub(r"[^a-z0-9]+", " ", ascii_name).split()
    return " ".join(t for t in tokens if t not in STOPWORDS)


def google_core_name(name: str | None) -> str:
    """'MELGA Autoservisas, padangos Vilniuje | Vilkpėdė' -> 'melga'"""
    return normalize(GOOGLE_NAME_SEPARATORS.split(name or "")[0])


@dataclass
class Company:
    code: str
    name: str
    nace_code: str | None  # latest Sodra activity code, None if never in Sodra


class Candidates:
    def __init__(self, companies: list[Company]):
        self.by_code = {c.code: c for c in companies}
        self.by_norm: dict[str, list[Company]] = defaultdict(list)
        for c in companies:
            norm = normalize(c.name)
            if norm:
                self.by_norm[norm].append(c)
        self.norm_names = list(self.by_norm)

    def search(self, core: str) -> list[tuple[str, float]]:
        hits = process.extract(core, self.norm_names, scorer=fuzz.token_sort_ratio, limit=3, score_cutoff=80)
        return [(name, score) for name, score, _ in hits]


def nace_agrees(company: Company | None, category_key: str) -> bool | None:
    if company is None or company.nace_code is None:
        return None
    return company.nace_code.startswith(CATEGORIES[category_key].nace_codes)


def nace_related(company: Company, category_key: str) -> bool:
    related = CATEGORIES[category_key].related_nace_codes
    return bool(company.nace_code and related and company.nace_code.startswith(related))


def match_by_name(core: str, category_key: str, cands: Candidates) -> dict:
    """Best name candidate with confidence; ambiguity and activity code lower confidence."""
    result = {"name_candidate": None, "name_score": None, "name_runner_up_score": None, "name_confidence": 0.0}
    if len(core) < 3:
        return {**result, "name_reason": "generic_or_short_name"}
    hits = cands.search(core)
    if not hits:
        return {**result, "name_reason": "no_name_candidate"}

    best_norm, best_score = hits[0]
    companies = cands.by_norm[best_norm]
    if len(companies) > 1:
        in_category = [c for c in companies if nace_agrees(c, category_key)]
        if len(in_category) != 1:
            return {**result, "name_score": best_score, "name_reason": "same_name_multiple_companies"}
        companies = in_category
    company = companies[0]
    runner_up = hits[1][1] if len(hits) > 1 else None
    result.update(name_candidate=company.code, name_score=best_score, name_runner_up_score=runner_up)

    if best_score < NAME_MIN_SCORE:
        return {**result, "name_reason": "name_score_too_low"}
    if runner_up is not None and best_score - runner_up < NAME_MIN_MARGIN:
        return {**result, "name_reason": "ambiguous_name"}
    agrees = nace_agrees(company, category_key)
    if agrees is None:
        confidence, reason = NAME_NO_NACE, "name_match_no_sodra_record"
    elif not agrees:
        confidence, reason = NAME_NACE_DISAGREES, "name_match_activity_code_disagrees"
    elif best_score == 100:
        confidence, reason = NAME_EXACT_NACE, "exact_name_and_activity_code"
    else:
        confidence, reason = NAME_HIGH_NACE, "close_name_and_activity_code"
    return {**result, "name_confidence": confidence, "name_reason": reason}


def match_by_website(scan: dict | None, core: str, cands: Candidates) -> dict:
    result = {"website_code": None, "website_page": None, "website_anchored": None}
    if not scan or not scan["codes"]:
        return result
    known = [c for c in scan["codes"] if c["code"] in cands.by_code]
    anchored = [c for c in known if c["anchored"]]
    pool = anchored or known
    if not pool:
        return result
    # Several codes on one site (e.g. group companies): prefer the one whose name fits the listing
    best = max(pool, key=lambda c: fuzz.token_sort_ratio(core, normalize(cands.by_code[c["code"]].name)))
    return {"website_code": best["code"], "website_page": best["page"], "website_anchored": best["anchored"]}


def decide(listing: dict, scan: dict | None, cands: Candidates) -> dict:
    core = google_core_name(listing["name"])
    by_name = match_by_name(core, listing["category"], cands)
    by_site = match_by_website(scan, core, cands)
    code, confidence, method, reason = None, 0.0, "unmatched", by_name["name_reason"]

    if by_site["website_code"]:
        code = by_site["website_code"]
        agrees = nace_agrees(cands.by_code[code], listing["category"])
        name_conf = by_name["name_confidence"]
        if code == by_name["name_candidate"] and name_conf > 0:
            confidence, method, reason = WEBSITE_AND_NAME, "website_code+name", "website code and name agree"
        elif name_conf >= MATCH_THRESHOLD:
            confidence, method = CONFLICT, "conflict"
            reason = f"website code {code} and name match {by_name['name_candidate']} disagree"
        elif by_site["website_anchored"]:
            method = "website_code"
            if agrees:
                confidence, reason = WEBSITE_ANCHORED_NACE, "labelled company code on website"
            elif agrees is None:
                confidence, reason = WEBSITE_ANCHORED_NO_SODRA, "labelled company code on website, no Sodra record"
            elif nace_related(cands.by_code[code], listing["category"]):
                confidence, reason = WEBSITE_ANCHORED_RELATED, "labelled company code on website, related industry"
            else:
                confidence = WEBSITE_ANCHORED_OTHER_INDUSTRY
                reason = "website code belongs to a company in another industry"
        else:
            confidence = WEBSITE_LOOSE_NACE if agrees else WEBSITE_LOOSE
            method, reason = "website_code_unlabelled", "unlabelled 9-digit code on website"
    elif by_name["name_confidence"] > 0:
        code, confidence, method = by_name["name_candidate"], by_name["name_confidence"], "name"

    company = cands.by_code.get(code) if code else None
    return {
        "run_id": listing["run_id"],
        "place_id": listing["place_id"],
        "category": listing["category"],
        "company_code": code,
        "company_name": company.name if company else None,
        "confidence": confidence,
        "accepted": confidence >= MATCH_THRESHOLD,
        "method": method,
        "reason": reason,
        "nace_code": company.nace_code if company else None,
        "nace_agrees": nace_agrees(company, listing["category"]),
        "google_core_name": core,
        **by_site,
        "name_candidate": by_name["name_candidate"],
        "name_score": by_name["name_score"],
        "name_runner_up_score": by_name["name_runner_up_score"],
        "name_confidence": by_name["name_confidence"],
    }


def load_candidates(con) -> Candidates:
    rows = con.execute(
        """
        WITH latest AS (
            SELECT company_code, nace_code FROM sodra_monthly
            QUALIFY row_number() OVER (PARTITION BY company_code ORDER BY month DESC) = 1
        )
        SELECT r.company_code, r.name, l.nace_code
        FROM registry_entities r LEFT JOIN latest l USING (company_code)
        WHERE r.deregistered_on IS NULL
        """
    ).fetchall()
    return Candidates([Company(*row) for row in rows])


def match_listings(run_id: str) -> int:
    with db.connect() as con:
        db.init_schema(con)
        listings = con.execute(
            "SELECT run_id, place_id, category, name, website FROM google_listings WHERE run_id = ?", [run_id]
        ).fetchall()
        listings = [dict(zip(("run_id", "place_id", "category", "name", "website"), r)) for r in listings]
        cands = load_candidates(con)
    log.info("Matching %d listings against %d active entities", len(listings), len(cands.by_code))

    scans = scan_websites([l["website"] for l in listings if l["website"]])
    rows = [decide(l, scans.get(clean_url(l["website"])) if l["website"] else None, cands) for l in listings]
    log_agreement(rows)

    with db.connect() as con:
        con.execute("DELETE FROM matches WHERE run_id = ?", [run_id])
        if rows:
            columns = list(rows[0])
            con.executemany(
                f"INSERT INTO matches ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                [list(r.values()) for r in rows],
            )
    accepted = sum(r["accepted"] for r in rows)
    log.info("Matched %d of %d listings (confidence >= %.2f)", accepted, len(rows), MATCH_THRESHOLD)
    return accepted


def log_agreement(rows: list[dict]) -> None:
    """How often an accepted name match points to the same company as a labelled website code."""
    checked = [r for r in rows if r["website_anchored"] and r["name_confidence"] >= MATCH_THRESHOLD]
    if checked:
        agree = sum(r["name_candidate"] == r["website_code"] for r in checked)
        log.info("Name vs website code agreement: %d/%d (%.0f%%)", agree, len(checked), 100 * agree / len(checked))
