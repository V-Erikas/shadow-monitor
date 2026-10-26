import zipfile

import duckdb
import pytest

from shadow import db
from shadow.collect import registry, sodra

SODRA_HEADER = (
    "Draudėjo kodas (code);Juridinių asmenų registro kodas (jarCode);Pavadinimas (name);"
    "Savivaldybė, kurioje registruota(municipality);Ekonominės veiklos rūšies kodas(ecoActCode);"
    "Ekonominės veiklos rūšies kodas EVRK(ecoActCodeStr);Ekonominės veiklos rūšies pavadinimas(ecoActName);"
    "Mėnuo (month);Vidutinis darbo užmokestis (avgWage);Apdraustųjų skaičius (numInsured);"
    "Vidutinis darbo užmokestis II (avgWage2);Apdraustųjų skaičius II (numInsured2);"
    "Valstybinio socialinio draudimo įmoka (tax)"
)
SODRA_ROWS = [
    '1;305636261;"UAB ARBATOS SOMELJĖ";"Vilniaus m. sav.";563000;56.30.00;"Gėrimų teikimas";202609;1036.22;9;;0;2510.18',
    '2;305740153;"MB ŠAUKŠTUKAS";"Vilniaus m. sav.";563000;56.30.00;"Gėrimų teikimas";202609;;1;;0;',
    '3;;"LATVIJOS AMBASADA";"Vilniaus m. sav.";;;"";202609;;2;;0;',
]


@pytest.fixture
def con():
    connection = duckdb.connect()
    db.init_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def sodra_csv(tmp_path):
    path = tmp_path / "monthly-2026.csv"
    path.write_text("\n".join([SODRA_HEADER, *SODRA_ROWS]) + "\n", encoding="utf-8")
    return path


def test_sodra_columns_maps_english_keys(sodra_csv):
    cols = sodra.columns(sodra_csv)
    assert cols["numInsured"] == "Apdraustųjų skaičius (numInsured)"
    assert cols["municipality"] == "Savivaldybė, kurioje registruota(municipality)"


def test_sodra_insert_skips_rows_without_company_code(con, sodra_csv):
    assert sodra.insert(con, [sodra_csv]) == 2
    rows = con.execute(
        "SELECT company_code, month, nace_code, insured, avg_wage FROM sodra_monthly ORDER BY company_code"
    ).fetchall()
    assert rows[0][0] == "305636261"
    assert str(rows[0][1]) == "2026-09-01"
    assert rows[0][2:] == ("56.30.00", 9, 1036.22)
    assert rows[1][3:] == (1, None)  # wage hidden for < 3 insured


def test_sodra_older_header_without_nace_string(con, tmp_path):
    header = SODRA_HEADER.replace("Ekonominės veiklos rūšies kodas EVRK(ecoActCodeStr);", "")
    row = '1;305636261;"UAB X";"Vilniaus m. sav.";452000;"Remontas";202512;1500;5;;0;900'
    path = tmp_path / "monthly-2025.csv"
    path.write_text(f"{header}\n{row}\n", encoding="utf-8")
    sodra.insert(con, [path])
    assert con.execute("SELECT nace_code FROM sodra_monthly").fetchone()[0] == "45.20.00"


def test_sodra_insert_replaces_previous_load(con, sodra_csv):
    sodra.insert(con, [sodra_csv])
    assert sodra.insert(con, [sodra_csv]) == 2


def test_sodra_duplicate_keys_roll_back(con, sodra_csv):
    sodra.insert(con, [sodra_csv])
    with pytest.raises(ValueError, match="Duplicate key"):
        sodra.insert(con, [sodra_csv, sodra_csv])
    assert con.execute("SELECT count(*) FROM sodra_monthly").fetchone()[0] == 2


def test_sodra_extract(tmp_path, sodra_csv):
    archive = tmp_path / "zipped" / "monthly-2026.csv.zip"
    archive.parent.mkdir()
    with zipfile.ZipFile(archive, "w") as z:
        z.write(sodra_csv, "monthly-2026.csv")
    assert sodra.extract(archive).read_text(encoding="utf-8").startswith("Draudėjo")


def test_registry_insert_resolves_form_and_status(con, tmp_path):
    entities = tmp_path / "ja.csv"
    entities.write_text(
        "_type,_id,ja_kodas,ja_pavadinimas,reg_data,isreg_data,forma._id,statusas._id,stat_data\n"
        'x,a,305636261,"UAB ""Arbatos someljė""",2020-10-07,,f1,s1,2020-10-07\n'
        "x,b,110000291,Old Co,1991-03-11,2002-03-22,f1,s2,2002-03-22\n",
        encoding="utf-8",
    )
    forms = tmp_path / "forma.csv"
    forms.write_text("_id,kodas,pavadinimas\nf1,310,Uždaroji akcinė bendrovė\n", encoding="utf-8")
    statuses = tmp_path / "statusas.csv"
    statuses.write_text("_id,kodas,pavadinimas\ns1,0,Teisinis statusas neįregistruotas\ns2,10,Išregistruotas\n", encoding="utf-8")

    assert registry.insert(con, entities, forms, statuses) == 2
    row = con.execute(
        "SELECT name, legal_form, status, registered_on, deregistered_on FROM registry_entities "
        "WHERE company_code = '305636261'"
    ).fetchone()
    assert row[:3] == ('UAB "Arbatos someljė"', "Uždaroji akcinė bendrovė", "Teisinis statusas neįregistruotas")
    assert str(row[3]) == "2020-10-07" and row[4] is None
