"""Visible activity: Google Places API (New) Text Search, or replay of a committed snapshot."""
import gzip
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from shadow import db
from shadow.config import CATEGORIES, GOOGLE_API_KEY, SNAPSHOT_DIR, VILNIUS_BBOX, Category

log = logging.getLogger(__name__)

GOOGLE_SNAPSHOT_DIR = SNAPSHOT_DIR / "google"

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = (
    "id",
    "displayName",
    "formattedAddress",
    "location",
    "types",
    "primaryType",
    "rating",
    "userRatingCount",
    "priceLevel",
    "regularOpeningHours",
    "websiteUri",
    "googleMapsUri",
    "businessStatus",
)
FIELD_MASK = ",".join(f"places.{f}" for f in FIELDS) + ",nextPageToken"
PAGE_SIZE = 20
MAX_RESULTS_PER_QUERY = 60
GRID_SIZE = 3
MAX_SPLIT_DEPTH = 2
# Default stays under the monthly free tier of the Text Search Enterprise SKU
MAX_REQUESTS = int(os.getenv("GOOGLE_MAX_REQUESTS", "900"))
RETRY_STATUSES = {429, 500, 502, 503, 504}

Box = tuple[float, float, float, float]  # south, west, north, east


class RequestBudgetExceeded(RuntimeError):
    pass


class PlacesClient:
    def __init__(self, api_key: str, max_requests: int = MAX_REQUESTS):
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY is not set (see .env.example)")
        self.session = requests.Session()
        self.session.headers.update(
            {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK, "Content-Type": "application/json"}
        )
        self.max_requests = max_requests
        self.requests_made = 0

    def _post(self, body: dict) -> dict:
        if self.requests_made >= self.max_requests:
            raise RequestBudgetExceeded(f"Request budget of {self.max_requests} reached")
        self.requests_made += 1
        for attempt in range(4):
            resp = self.session.post(SEARCH_URL, json=body, timeout=30)
            if resp.status_code not in RETRY_STATUSES:
                break
            time.sleep(2**attempt)
        resp.raise_for_status()
        return resp.json()

    def search(self, query: str, box: Box) -> list[dict]:
        south, west, north, east = box
        body = {
            "textQuery": query,
            "languageCode": "lt",
            "regionCode": "LT",
            "pageSize": PAGE_SIZE,
            "locationRestriction": {
                "rectangle": {
                    "low": {"latitude": south, "longitude": west},
                    "high": {"latitude": north, "longitude": east},
                }
            },
        }
        places: list[dict] = []
        while True:
            data = self._post(body)
            places.extend(data.get("places", []))
            token = data.get("nextPageToken")
            if not token or len(places) >= MAX_RESULTS_PER_QUERY:
                return places
            body["pageToken"] = token
            time.sleep(0.5)


def split(box: Box, n: int) -> list[Box]:
    south, west, north, east = box
    dlat, dlon = (north - south) / n, (east - west) / n
    return [
        (south + i * dlat, west + j * dlon, south + (i + 1) * dlat, west + (j + 1) * dlon)
        for i in range(n)
        for j in range(n)
    ]


def search_area(client: PlacesClient, query: str, box: Box, depth: int = 0) -> list[dict]:
    places = client.search(query, box)
    # API caps a query at 60 results, so dense cells are split to reach the rest
    if len(places) >= MAX_RESULTS_PER_QUERY and depth < MAX_SPLIT_DEPTH:
        for sub in split(box, 2):
            places.extend(search_area(client, query, sub, depth + 1))
    return places


def _week_minute(point: dict) -> int:
    return point["day"] * 1440 + point.get("hour", 0) * 60 + point.get("minute", 0)


def weekly_open_hours(opening_hours: dict | None) -> float | None:
    if not opening_hours or not opening_hours.get("periods"):
        return None
    total = 0
    for period in opening_hours["periods"]:
        start, end = period.get("open"), period.get("close")
        if start is None:
            continue
        if end is None:
            return 168.0
        # modulo handles periods that wrap past Saturday midnight
        total += (_week_minute(end) - _week_minute(start)) % (7 * 1440)
    return round(total / 60, 1)


def is_relevant(place: dict, category: Category) -> bool:
    # Primary type only: secondary types let in e.g. McDonald's as "cafe"
    in_category = place.get("primaryType") in category.google_types
    return in_category and "Vilnius" in place.get("formattedAddress", "")


def to_row(
    place: dict, category: Category, query: str, run_id: str, fetched_at: datetime, source: str = "live"
) -> dict:
    location = place.get("location", {})
    return {
        "run_id": run_id,
        "place_id": place["id"],
        "category": category.key,
        "name": place.get("displayName", {}).get("text"),
        "address": place.get("formattedAddress"),
        "lat": location.get("latitude"),
        "lon": location.get("longitude"),
        "primary_type": place.get("primaryType"),
        "types": place.get("types", []),
        "rating": place.get("rating"),
        "review_count": place.get("userRatingCount", 0),
        "price_level": place.get("priceLevel"),
        "weekly_open_hours": weekly_open_hours(place.get("regularOpeningHours")),
        "website": place.get("websiteUri"),
        "maps_url": place.get("googleMapsUri"),
        "business_status": place.get("businessStatus"),
        "search_query": query,
        "source": source,
        "fetched_at": fetched_at,
        "raw": json.dumps(place, ensure_ascii=False),
    }


def collect_listings(run_id: str) -> int:
    if GOOGLE_API_KEY:
        rows = fetch_live(run_id)
        write_snapshot(rows, GOOGLE_SNAPSHOT_DIR / f"{run_id}.jsonl.gz")
    else:
        rows = replay_snapshot(latest_snapshot(GOOGLE_SNAPSHOT_DIR), run_id)
    save(rows, run_id)
    return len(rows)


def fetch_live(run_id: str) -> list[dict]:
    client = PlacesClient(GOOGLE_API_KEY)
    fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)
    rows: dict[tuple[str, str], dict] = {}

    try:
        for category in CATEGORIES.values():
            for query in category.queries:
                for cell in split(VILNIUS_BBOX, GRID_SIZE):
                    for place in search_area(client, query, cell):
                        if is_relevant(place, category):
                            rows.setdefault(
                                (place["id"], category.key), to_row(place, category, query, run_id, fetched_at)
                            )
            log.info("%s done: %d listings total, %d requests", category.key, len(rows), client.requests_made)
    except RequestBudgetExceeded as exc:
        log.warning("%s; saving %d listings collected so far", exc, len(rows))

    return list(rows.values())


def write_snapshot(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            record = {
                "category": r["category"],
                "search_query": r["search_query"],
                "fetched_at": r["fetched_at"].isoformat(),
                "place": json.loads(r["raw"]),
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    log.info("Snapshot written to %s", path)


def latest_snapshot(directory: Path) -> Path:
    snapshots = sorted(directory.glob("*.jsonl.gz"))
    if not snapshots:
        raise RuntimeError(f"GOOGLE_API_KEY is not set and no snapshot found in {directory}")
    return snapshots[-1]


def replay_snapshot(path: Path, run_id: str) -> list[dict]:
    log.info("No GOOGLE_API_KEY: replaying snapshot %s", path.name)
    source = f"snapshot:{path.name}"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        records = [json.loads(line) for line in f]
    return [
        to_row(
            rec["place"],
            CATEGORIES[rec["category"]],
            rec["search_query"],
            run_id,
            datetime.fromisoformat(rec["fetched_at"]),
            source,
        )
        for rec in records
        if is_relevant(rec["place"], CATEGORIES[rec["category"]])
    ]


def save(rows: list[dict], run_id: str) -> None:
    with db.connect() as con:
        db.init_schema(con)
        con.execute("DELETE FROM google_listings WHERE run_id = ?", [run_id])
        if rows:
            columns = list(rows[0].keys())
            con.executemany(
                f"INSERT INTO google_listings ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                [list(r.values()) for r in rows],
            )
