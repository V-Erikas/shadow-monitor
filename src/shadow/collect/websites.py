"""Company codes published on business websites (footer or contacts page)."""
import gzip
import html
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib import robotparser
from urllib.parse import urljoin, urlparse, urlunparse

import requests

from shadow.collect.download import USER_AGENT
from shadow.config import SNAPSHOT_DIR

log = logging.getLogger(__name__)

CACHE_PATH = SNAPSHOT_DIR / "websites.jsonl.gz"
CACHE_MAX_AGE = timedelta(days=90)
# Bump when scanning logic changes so cached results are refreshed
SCANNER_VERSION = 2
MAX_SUBPAGES = 3
SKIP_DOMAINS = ("facebook.com", "instagram.com", "linktr.ee", "wolt.com", "google.com", "tiktok.com", "booking.com")
TIMEOUT = (5, 10)
MAX_BYTES = 2_000_000
WORKERS = 8

ANCHORED_CODE = re.compile(
    r"(?:įmonės\s+kodas|imonės\s+kodas|įmonės\s+k\.|į\.\s?k\.|im\.\s?k\.|juridinio\s+asmens\s+kodas|"
    r"company\s+code|registration\s+(?:code|number|no\.?)|reg\.\s?(?:kodas|nr\.?)|kodas)"
    r"\s*[:.\-]?\s*([13]\d{8})(?!\d)",
    re.IGNORECASE,
)
# Not preceded by letters, so VAT codes like LT123456789 are ignored
LOOSE_CODE = re.compile(r"(?<![\dA-Za-z])([13]\d{8})(?!\d)")
LINK = re.compile(r"<a\s[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
# LT sites usually publish company details on contacts, privacy policy or terms pages
SUBPAGE_HINT = re.compile(r"kontakt|contact|rekvizit|apie|about|privatum|privacy|taisykl|terms|slapuk|cookie", re.IGNORECASE)


def clean_url(url: str) -> str:
    p = urlparse(url.strip())
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path or "/", "", "", ""))


def page_text(raw_html: str) -> str:
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", raw_html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text))


def extract_codes(raw_html: str, page: str) -> list[dict]:
    text = page_text(raw_html)
    anchored = set(ANCHORED_CODE.findall(text))
    loose = set(LOOSE_CODE.findall(text)) - anchored
    return [{"code": c, "anchored": True, "page": page} for c in sorted(anchored)] + [
        {"code": c, "anchored": False, "page": page} for c in sorted(loose)
    ]


def subpage_links(raw_html: str, base: str) -> list[str]:
    host = urlparse(base).netloc
    links: list[str] = []
    for href, label in LINK.findall(raw_html):
        if SUBPAGE_HINT.search(href) or SUBPAGE_HINT.search(label):
            target = urljoin(base, href)
            if urlparse(target).netloc == host and target.startswith(("http://", "https://")) and target not in links:
                links.append(target)
    return links[:MAX_SUBPAGES]


def _fetch(session: requests.Session, url: str) -> str | None:
    with session.get(url, timeout=TIMEOUT, stream=True, allow_redirects=True) as resp:
        content_type = resp.headers.get("Content-Type", "")
        if resp.status_code != 200 or "html" not in content_type:
            return None
        content = resp.raw.read(MAX_BYTES, decode_content=True)
        # requests assumes ISO-8859-1 when no charset is declared; most LT sites are UTF-8
        encoding = resp.encoding if "charset" in content_type.lower() else "utf-8"
        return content.decode(encoding or "utf-8", errors="replace")


def _robots_allows(session: requests.Session, url: str) -> bool:
    p = urlparse(url)
    try:
        resp = session.get(f"{p.scheme}://{p.netloc}/robots.txt", timeout=TIMEOUT)
    except requests.RequestException:
        return True
    if resp.status_code != 200:
        return True
    parser = robotparser.RobotFileParser()
    parser.parse(resp.text.splitlines())
    return parser.can_fetch(USER_AGENT, url)


def scan(url: str) -> dict:
    record = {
        "url": url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "scanner_version": SCANNER_VERSION,
        "status": "ok",
        "codes": [],
    }
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return {**record, "status": "invalid_url"}
    if any(p.netloc == d or p.netloc.endswith("." + d) for d in SKIP_DOMAINS):
        return {**record, "status": "skipped_domain"}

    with requests.Session() as session:
        session.headers["User-Agent"] = USER_AGENT
        try:
            if not _robots_allows(session, url):
                return {**record, "status": "robots_disallowed"}
            home = _fetch(session, url)
            if home is None:
                return {**record, "status": "no_html"}
            codes = extract_codes(home, url)
            for link in subpage_links(home, url):
                if any(c["anchored"] for c in codes):
                    break
                if _robots_allows(session, link):
                    page = _fetch(session, link)
                    if page:
                        codes += extract_codes(page, link)
        except requests.RequestException as exc:
            return {**record, "status": f"error:{type(exc).__name__}"}
    return {**record, "codes": codes}


def load_cache() -> dict[str, dict]:
    if not CACHE_PATH.exists():
        return {}
    with gzip.open(CACHE_PATH, "rt", encoding="utf-8") as f:
        return {rec["url"]: rec for rec in map(json.loads, f)}


def save_cache(cache: dict[str, dict]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(CACHE_PATH, "wt", encoding="utf-8") as f:
        for rec in sorted(cache.values(), key=lambda r: r["url"]):
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def scan_websites(urls: list[str]) -> dict[str, dict]:
    """Return scan records keyed by cleaned URL, reusing cached scans younger than CACHE_MAX_AGE."""
    cache = load_cache()
    now = datetime.now(timezone.utc)
    wanted = {clean_url(u) for u in urls}
    todo = [
        u
        for u in wanted
        if u not in cache
        or cache[u].get("scanner_version") != SCANNER_VERSION
        or now - datetime.fromisoformat(cache[u]["fetched_at"]) > CACHE_MAX_AGE
    ]
    log.info("Websites: %d unique, %d cached, %d to scan", len(wanted), len(wanted) - len(todo), len(todo))
    if todo:
        with ThreadPoolExecutor(WORKERS) as pool:
            for rec in pool.map(scan, todo):
                cache[rec["url"]] = rec
        save_cache(cache)
    return {u: cache[u] for u in wanted}
