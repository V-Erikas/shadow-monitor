"""Declared activity: Sodra monthly insured persons and wages (atvira.sodra.lt)."""
import logging
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import requests

from shadow import db
from shadow.collect.download import download
from shadow.config import DATA_DIR

log = logging.getLogger(__name__)

URL = "https://atvira.sodra.lt/imones/downloads/{year}/monthly-{year}.csv.zip"
RAW_DIR = DATA_DIR / "raw" / "sodra"
# Current + previous year: enough for trends and flower-shop seasonality
YEARS_BACK = 1


def load_sodra() -> int:
    this_year = date.today().year
    csv_files = []
    for year in range(this_year - YEARS_BACK, this_year + 1):
        max_age = timedelta(days=1) if year == this_year else timedelta(days=30)
        try:
            archive = download(URL.format(year=year), RAW_DIR / f"monthly-{year}.csv.zip", max_age)
        except requests.HTTPError as exc:
            # In early January the new year's file may not be published yet
            if year == this_year and exc.response is not None and exc.response.status_code == 404:
                log.warning("Sodra file for %d not published yet", year)
                continue
            raise
        csv_files.append(extract(archive))

    with db.connect() as con:
        db.init_schema(con)
        return insert(con, csv_files)


def extract(archive: Path) -> Path:
    with zipfile.ZipFile(archive) as z:
        member = z.namelist()[0]
        target = archive.parent / member
        if not target.exists() or target.stat().st_mtime < archive.stat().st_mtime:
            z.extract(member, archive.parent)
    return target


def columns(csv_path: Path) -> dict[str, str]:
    """Map the English key in each header, e.g. '... (numInsured)' -> 'numInsured', to the full header."""
    with open(csv_path, encoding="utf-8-sig") as f:
        header = f.readline().rstrip("\r\n").split(";")
    mapping = {}
    for col in header:
        match = re.search(r"\((\w+)\)\s*$", col)
        if match:
            mapping[match.group(1)] = col
    return mapping


def insert(con: duckdb.DuckDBPyConnection, csv_files: list[Path]) -> int:
    loaded_at = datetime.now(timezone.utc).replace(tzinfo=None)
    inserts = []
    for path in csv_files:
        c = {key: f'"{name}"' for key, name in columns(path).items()}
        # Older files lack ecoActCodeStr, so derive "56.30.00" from numeric 563000 for every year
        num = f"lpad({c['ecoActCode']}, 6, '0')"
        nace = (
            f"CASE WHEN length({c['ecoActCode']}) >= 5 "
            f"THEN substr({num}, 1, 2) || '.' || substr({num}, 3, 2) || '.' || substr({num}, 5, 2) "
            f"ELSE {c.get('ecoActCodeStr', 'NULL')} END"
        )
        sql = f"""
            INSERT INTO sodra_monthly
            SELECT
                strptime({c['month']}, '%Y%m')::DATE,
                {c['jarCode']},
                {c['code']},
                {c['name']},
                {c['municipality']},
                {nace},
                {c['ecoActName']},
                TRY_CAST({c['numInsured']} AS INTEGER),
                TRY_CAST({c['avgWage']} AS DOUBLE),
                TRY_CAST({c['numInsured2']} AS INTEGER),
                TRY_CAST({c['avgWage2']} AS DOUBLE),
                TRY_CAST({c['tax']} AS DOUBLE),
                ?,
                ?
            FROM read_csv('{path}', delim = ';', header = true, all_varchar = true)
            WHERE {c['jarCode']} IS NOT NULL
        """
        inserts.append((sql, [path.name, loaded_at]))
    count = db.replace_table(con, "sodra_monthly", "month, company_code", inserts)
    months = con.execute("SELECT count(DISTINCT month) FROM sodra_monthly").fetchone()[0]
    log.info("Sodra: %d rows over %d months", count, months)
    return count

