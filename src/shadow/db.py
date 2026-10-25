import duckdb

from shadow.config import DB_PATH


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH), read_only=read_only)


# All timestamps are UTC
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      VARCHAR PRIMARY KEY,
    started_at  TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    status      VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS google_listings (
    run_id            VARCHAR NOT NULL,
    place_id          VARCHAR NOT NULL,
    category          VARCHAR NOT NULL,
    name              VARCHAR,
    address           VARCHAR,
    lat               DOUBLE,
    lon               DOUBLE,
    primary_type      VARCHAR,
    types             VARCHAR[],
    rating            DOUBLE,
    review_count      INTEGER,
    price_level       VARCHAR,
    weekly_open_hours DOUBLE,
    website           VARCHAR,
    maps_url          VARCHAR,
    business_status   VARCHAR,
    search_query      VARCHAR,
    source            VARCHAR NOT NULL,
    fetched_at        TIMESTAMP NOT NULL,
    raw               JSON NOT NULL,
    PRIMARY KEY (run_id, place_id, category)
);
"""


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA)
