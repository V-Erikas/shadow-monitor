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

-- Reference tables are fully reloaded each run; uniqueness is checked by the loaders because
-- DuckDB rejects delete + re-insert of the same primary key within one transaction
CREATE TABLE IF NOT EXISTS registry_entities (
    company_code    VARCHAR NOT NULL,
    name            VARCHAR,
    legal_form      VARCHAR,
    status          VARCHAR,
    registered_on   DATE,
    deregistered_on DATE,
    status_since    DATE,
    loaded_at       TIMESTAMP NOT NULL
);

-- All municipalities: a Vilnius shop can belong to a company registered elsewhere
CREATE TABLE IF NOT EXISTS sodra_monthly (
    month        DATE NOT NULL,
    company_code VARCHAR NOT NULL,
    insurer_code VARCHAR,
    name         VARCHAR,
    municipality VARCHAR,
    nace_code    VARCHAR,
    nace_name    VARCHAR,
    insured      INTEGER,
    avg_wage     DOUBLE,  -- hidden by Sodra when insured < 3
    insured_2    INTEGER,
    avg_wage_2   DOUBLE,
    social_tax   DOUBLE,
    source_file  VARCHAR NOT NULL,
    loaded_at    TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    run_id               VARCHAR NOT NULL,
    place_id             VARCHAR NOT NULL,
    category             VARCHAR NOT NULL,
    company_code         VARCHAR,  -- null when unmatched
    company_name         VARCHAR,
    confidence           DOUBLE NOT NULL,
    accepted             BOOLEAN NOT NULL,
    method               VARCHAR NOT NULL,
    reason               VARCHAR,
    nace_code            VARCHAR,
    nace_agrees          BOOLEAN,
    google_core_name     VARCHAR,
    website_code         VARCHAR,
    website_page         VARCHAR,
    website_anchored     BOOLEAN,
    name_candidate       VARCHAR,
    name_score           DOUBLE,
    name_runner_up_score DOUBLE,
    name_confidence      DOUBLE,
    PRIMARY KEY (run_id, place_id, category)
);
"""


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA)


def replace_table(con: duckdb.DuckDBPyConnection, table: str, key: str, inserts: list[tuple[str, list]]) -> int:
    """Atomically replace a reference table's contents; roll back if key is not unique."""
    con.execute("BEGIN")
    try:
        con.execute(f"DELETE FROM {table}")
        for sql, params in inserts:
            con.execute(sql, params)
        dup = con.execute(f"SELECT {key} FROM {table} GROUP BY ALL HAVING count(*) > 1 LIMIT 1").fetchone()
        if dup:
            raise ValueError(f"Duplicate key in {table}: {dup}")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
