import duckdb

from shadow.config import DB_PATH


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH), read_only=read_only)


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    raise NotImplementedError("Step 5")
