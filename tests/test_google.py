from datetime import datetime

import pytest

from shadow.collect import google
from shadow.config import CATEGORIES

COFFEE = CATEGORIES["coffee_shop"]

PLACE = {
    "id": "ChIJ123",
    "displayName": {"text": "Kavinė Pupa"},
    "formattedAddress": "Gedimino pr. 9, 01103 Vilnius, Lietuva",
    "location": {"latitude": 54.687, "longitude": 25.279},
    "types": ["cafe", "food", "point_of_interest"],
    "primaryType": "cafe",
    "rating": 4.6,
    "userRatingCount": 1850,
    "businessStatus": "OPERATIONAL",
    "regularOpeningHours": {
        "periods": [
            {"open": {"day": d, "hour": 8, "minute": 0}, "close": {"day": d, "hour": 18, "minute": 0}}
            for d in range(1, 6)
        ]
    },
}


def test_weekly_open_hours_weekdays():
    assert google.weekly_open_hours(PLACE["regularOpeningHours"]) == 50.0


def test_weekly_open_hours_always_open():
    assert google.weekly_open_hours({"periods": [{"open": {"day": 0, "hour": 0}}]}) == 168.0


def test_weekly_open_hours_wraps_past_week_end():
    sat_night = {"open": {"day": 6, "hour": 22}, "close": {"day": 0, "hour": 2}}
    assert google.weekly_open_hours({"periods": [sat_night]}) == 4.0


def test_weekly_open_hours_missing():
    assert google.weekly_open_hours(None) is None


def test_split_covers_box_without_gaps():
    cells = google.split((0.0, 0.0, 3.0, 3.0), 3)
    assert len(cells) == 9
    assert sum((n - s) * (e - w) for s, w, n, e in cells) == pytest.approx(9.0)


def test_is_relevant_filters_type_and_city():
    assert google.is_relevant(PLACE, COFFEE)
    assert not google.is_relevant({**PLACE, "primaryType": "fast_food_restaurant"}, COFFEE)
    assert not google.is_relevant({**PLACE, "formattedAddress": "Pagiriai, Vilniaus r. sav."}, COFFEE)


def test_to_row_maps_fields():
    row = google.to_row(PLACE, COFFEE, "kavinė Vilnius", "run1", datetime(2026, 10, 1))
    assert row["place_id"] == "ChIJ123"
    assert row["name"] == "Kavinė Pupa"
    assert row["review_count"] == 1850
    assert row["weekly_open_hours"] == 50.0
    assert '"Kavinė Pupa"' in row["raw"]
    assert row["source"] == "live"


def test_snapshot_round_trip(tmp_path):
    row = google.to_row(PLACE, COFFEE, "kavinė Vilnius", "run1", datetime(2026, 10, 1))
    google.write_snapshot([row], tmp_path / "run1.jsonl.gz")

    path = google.latest_snapshot(tmp_path)
    replayed = google.replay_snapshot(path, "run2")

    assert len(replayed) == 1
    assert replayed[0]["run_id"] == "run2"
    assert replayed[0]["source"] == "snapshot:run1.jsonl.gz"
    for key in ("place_id", "category", "review_count", "weekly_open_hours", "fetched_at", "raw"):
        assert replayed[0][key] == row[key]


def test_latest_snapshot_missing(tmp_path):
    with pytest.raises(RuntimeError, match="no snapshot"):
        google.latest_snapshot(tmp_path)


class FakeClient:
    def __init__(self, dense_depths):
        self.dense_depths = dense_depths
        self.calls = 0

    def search(self, query, box):
        depth = self.calls and 1
        self.calls += 1
        size = google.MAX_RESULTS_PER_QUERY if depth in self.dense_depths else 5
        return [{"id": f"{self.calls}-{i}"} for i in range(size)]


def test_search_area_splits_dense_cell():
    client = FakeClient(dense_depths={0})
    places = google.search_area(client, "q", (0, 0, 1, 1))
    assert client.calls == 1 + 4
    assert len(places) == 60 + 4 * 5
