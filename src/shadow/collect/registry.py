"""Legal entities: Registrų centras JAR via the data.gov.lt open data API."""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

from shadow import db
from shadow.collect.download import download
from shadow.config import DATA_DIR

log = logging.getLogger(__name__)

BASE_URL = "https://get.data.gov.lt/datasets/gov/rc/jar"
ENTITIES_URL = f"{BASE_URL}/iregistruoti/JuridinisAsmuo/:format/csv"
FORMS_URL = f"{BASE_URL}/formos_statusai/Forma/:format/csv"
STATUSES_URL = f"{BASE_URL}/formos_statusai/Statusas/:format/csv"
RAW_DIR = DATA_DIR / "raw" / "jar"
MAX_AGE = timedelta(days=1)


def load_registry() -> int:
    entities = download(ENTITIES_URL, RAW_DIR / "juridinis_asmuo.csv", MAX_AGE)
    forms = download(FORMS_URL, RAW_DIR / "forma.csv", MAX_AGE)
    statuses = download(STATUSES_URL, RAW_DIR / "statusas.csv", MAX_AGE)
    with db.connect() as con:
        db.init_schema(con)
        return insert(con, entities, forms, statuses)


def insert(con: duckdb.DuckDBPyConnection, entities: Path, forms: Path, statuses: Path) -> int:
    loaded_at = datetime.now(timezone.utc).replace(tzinfo=None)
    sql = f"""
        INSERT INTO registry_entities
        SELECT
            lpad(j.ja_kodas, 9, '0'),
            j.ja_pavadinimas,
            f.pavadinimas,
            s.pavadinimas,
            TRY_CAST(j.reg_data AS DATE),
            TRY_CAST(j.isreg_data AS DATE),
            TRY_CAST(j.stat_data AS DATE),
            ?
        FROM read_csv('{entities}', header = true, all_varchar = true) j
        LEFT JOIN read_csv('{forms}', header = true, all_varchar = true) f ON f._id = j."forma._id"
        LEFT JOIN read_csv('{statuses}', header = true, all_varchar = true) s ON s._id = j."statusas._id"
        WHERE j.ja_kodas IS NOT NULL
    """
    count = db.replace_table(con, "registry_entities", "company_code", [(sql, [loaded_at])])
    log.info("Registry: %d legal entities", count)
    return count

