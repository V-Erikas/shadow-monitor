import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
SNAPSHOT_DIR = ROOT / "snapshots"
DB_PATH = OUTPUT_DIR / "shadow.duckdb"

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")

VILNIUS_BBOX = (54.567, 25.024, 54.832, 25.481)

# Listings below this match confidence are never scored
MATCH_THRESHOLD = 0.8


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    google_types: tuple[str, ...]
    queries: tuple[str, ...]
    nace_codes: tuple[str, ...]
    min_reviews: int
    # Owners sometimes registered under a neighbouring activity (e.g. coffee chain run by a retailer)
    related_nace_codes: tuple[str, ...] = ()


CATEGORIES: dict[str, Category] = {
    c.key: c
    for c in (
        Category(
            key="coffee_shop",
            label="Coffee shops",
            google_types=("cafe", "coffee_shop"),
            queries=("kavinė Vilnius", "kava Vilnius", "coffee shop Vilnius"),
            nace_codes=("56.30", "56.11"),
            min_reviews=200,
            related_nace_codes=("56.", "47.11", "47.2", "10.7", "10.83"),
        ),
        Category(
            key="car_repair",
            label="Car repair / tyre shops",
            google_types=("car_repair", "tire_shop"),
            queries=("autoservisas Vilnius", "padangų servisas Vilnius"),
            nace_codes=("95.31",),
            min_reviews=100,
            related_nace_codes=("45.", "46.72", "47.81", "47.82", "33.12", "33.17"),
        ),
        Category(
            key="florist",
            label="Flower shops",
            google_types=("florist",),
            queries=("gėlių parduotuvė Vilnius", "gėlės Vilnius"),
            nace_codes=("47.76",),
            min_reviews=100,
            related_nace_codes=("46.22", "47.12", "01.19"),
        ),
    )
}
