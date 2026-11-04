import pandas as pd
import pytest

from app.constants import district_crosswalk
from app.modules.scraping import scoring


def numbers(rows=38, cols=4):
    return pd.DataFrame({f"c{j}": [i * (j + 1) for i in range(rows)] for j in range(cols)})


def test_layout_one_column_nav():
    assert scoring.looks_like_layout(pd.DataFrame({"x": ["Home", "About Us", "Contact Us"]}))


def test_layout_nav_words_in_two_columns():
    df = pd.DataFrame({"a": ["Home", "Sitemap", "FAQ"], "b": ["About Us", "Login", "Help"]})
    assert scoring.looks_like_layout(df)


def test_layout_single_row():
    assert scoring.looks_like_layout(pd.DataFrame({"a": [1], "b": [2]}))


def test_real_table_not_layout():
    assert not scoring.looks_like_layout(numbers())


def test_numeric_share_handles_commas_and_percent():
    df = pd.DataFrame({"v": ["1,234", "56%", "x", "NA"]})
    assert scoring.numeric_share(df) == 0.5


def test_joinable_with_state_column():
    df = pd.DataFrame({"State": ["Bihar", "Bihar", "Bihar"], "District": ["Patna", "Gaya", "Nowhere"],
                       "Beds": [1, 2, 3]})
    j = scoring.joinable(df)
    assert j == {"place_column": "District", "state_column": "State", "level": "district",
                 "matched": 2, "total": 3, "unmatched": ["Nowhere"]}


def test_joinable_district_only():
    j = scoring.joinable(pd.DataFrame({"District": ["Patna", "Gaya"], "Beds": [1, 2]}))
    assert j["state_column"] is None and j["level"] == "district" and j["matched"] == 2


def test_joinable_state_level():
    j = scoring.joinable(pd.DataFrame({"State Name": ["Bihar", "Kerala"], "v": [1, 2]}))
    assert j["level"] == "state" and j["matched"] == 2 and j["place_column"] == "State Name"


def test_joinable_none():
    assert scoring.joinable(pd.DataFrame({"Year": [2020, 2021], "v": [1, 2]})) is None


def test_summary_row_not_counted():
    j = scoring.joinable(pd.DataFrame({"State": ["Bihar", "Bihar"], "District": ["_Bihar", "Patna"]}))
    assert j["total"] == 1 and j["matched"] == 1


def test_unmatched_capped_at_20():
    df = pd.DataFrame({"State": ["Bihar"] * 30, "District": [f"Nowhere {i}" for i in range(30)]})
    assert len(scoring.joinable(df)["unmatched"]) == 20


def test_find_district_across_states():
    hits = district_crosswalk.find_district("Aurangabad")
    assert {h["state"] for h in hits} >= {"Bihar", "Maharashtra"}
    assert district_crosswalk.find_district("Nowhere") == []


def test_personal_data():
    df = pd.DataFrame({
        "Mobile": ["9876543210", "+91 98765 43211", "98765-43212"],
        "Email": ["a@b.in", "c@d.gov.in", "e@f.com"],
        "Name of Officer": ["A", "B", "C"],
        "District Name": ["Patna", "Gaya", "Araria"],
        "Facility Name": ["PHC 1", "PHC 2", "PHC 3"],
        "Beds": [1, 2, 3],
    })
    assert scoring.personal_data_columns(df) == ["Mobile", "Email", "Name of Officer"]


def test_phone_values_detected_under_neutral_header():
    df = pd.DataFrame({"Helpline": ["9876543210", "9123456789", "8123456789"]})
    assert scoring.personal_data_columns(df) == ["Helpline"]


def test_score_shape():
    s = scoring.score(numbers())
    assert set(s) == {"looks_like_layout", "numeric_share", "joinable", "personal_data",
                      "duplicate_of", "truncated"}
    assert s["duplicate_of"] is None and s["truncated"] is False and s["numeric_share"] == 1.0


GOOD = {"looks_like_layout": False, "duplicate_of": None, "numeric_share": 0.8, "personal_data": []}


def test_suggest_keep():
    assert scoring.suggest(GOOD, rows=38, columns=4) == "keep"


@pytest.mark.parametrize("change,rows,cols", [
    ({"looks_like_layout": True}, 38, 4),
    ({"duplicate_of": "f1"}, 38, 4),
    ({}, 2, 4),
    ({}, 38, 1),
    ({"numeric_share": 0.0}, 38, 4),
    ({"personal_data": ["Mobile"]}, 38, 4),
])
def test_suggest_skip_rules(change, rows, cols):
    assert scoring.suggest({**GOOD, **change}, rows=rows, columns=cols) == "skip"


def test_hash_html_vs_csv_equal():
    html = pd.DataFrame({"District": ["Patna", "Gaya"], "Beds": [1234.0, 56.0]})
    csv = pd.DataFrame({"district_name": ["Patna ", "Gaya"], "Total Beds": ["1,234", "56"]})
    assert scoring.table_hash(html) == scoring.table_hash(csv)
    other = pd.DataFrame({"District": ["Patna", "Gaya"], "Beds": [1, 2]})
    assert scoring.table_hash(other) != scoring.table_hash(html)


def test_duplicates_prefer_files():
    items = [("h1", "html_table", "X"), ("f1", "file_table", "X"), ("h2", "html_table", "Y"),
             ("h3", "html_table", "Y")]
    assert scoring.mark_duplicates(items) == {"h1": "f1", "h3": "h2"}


def test_score_large_table_is_quick():
    import time

    n = 300_000
    df = pd.DataFrame({"State": ["Bihar"] * n, "District": [f"D{i % 40}" for i in range(n)],
                       "a": [str(i) for i in range(n)], "b": [f"{i},000" for i in range(n)],
                       "c": ["NA"] * n, "d": [i * 1.5 for i in range(n)]})
    start = time.perf_counter()
    s = scoring.score(df)
    assert time.perf_counter() - start < 2.0
    assert s["joinable"]["total"] == 40 and s["numeric_share"] == 0.5  # a, b, d of 6 columns


def test_feed_wins_over_rendered_table_duplicate():
    assert scoring.mark_duplicates([("a", "rendered_table", "h"), ("b", "feed", "h")]) == {"a": "b"}
    assert scoring.mark_duplicates(
        [("a", "rendered_table", "h"), ("b", "feed", "h"), ("c", "file_table", "h")]) == {"a": "c", "b": "c"}
