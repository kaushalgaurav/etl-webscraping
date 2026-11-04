import pandas as pd
import pytest

from app.modules.scraping import store


def test_mixed_column_round_trips():
    df = pd.DataFrame({"District": ["Patna", "Gaya", "Araria", "Arwal", "Banka"],
                       "Beds": [1, "NA", "-", "1,234", None]})
    rel = store.write_table("scanmixed", "f1", store.prepare_for_parquet(df))
    back = store.read_table("scanmixed", rel)
    assert back["Beds"].tolist() == ["1", "NA", "-", "1,234", None]
    assert rel == "tables/f1.parquet"


def test_numeric_columns_stay_numeric():
    df = pd.DataFrame({"a": [1, 2], "b": [1.5, None]})
    back = store.read_table("scannum", store.write_table("scannum", "f1", store.prepare_for_parquet(df)))
    assert back["a"].tolist() == [1, 2] and back["b"].iloc[0] == 1.5


def test_duplicate_and_long_column_names():
    df = pd.DataFrame([[1, 2, 3]], columns=["A", "A", "x" * 80])
    prepared = store.prepare_for_parquet(df)
    assert len(set(prepared.columns)) == 3
    store.write_table("scandup", "f1", prepared)


def test_resolve_rejects_escape():
    with pytest.raises(ValueError):
        store.resolve("scanx", "../../etc/passwd")
    with pytest.raises(ValueError):
        store.resolve("../other", "a.txt")


def test_write_bytes_safe_name():
    rel = store.write_bytes("scanbytes", 3, "../a b?.csv", b"x")
    path = store.resolve("scanbytes", rel)
    assert path.read_bytes() == b"x" and rel.startswith("files/003_")
    assert store.scan_dir("scanbytes").resolve() in path.parents


def test_write_text_and_delete():
    rel = store.write_text("scandel", "t1", "पटना text")
    assert store.resolve("scandel", rel).read_text(encoding="utf-8") == "पटना text"
    store.delete_scan_dir("scandel")
    assert not store.scan_dir("scandel").exists()
    store.delete_scan_dir("scandel")  # deleting twice is fine
