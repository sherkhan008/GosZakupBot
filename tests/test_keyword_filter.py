from pathlib import Path

import pytest

from app.filters.keyword_filter import KeywordFilter

KEYWORDS_PATH = Path(__file__).resolve().parent.parent / "config" / "keywords.yaml"


@pytest.fixture(scope="module")
def keyword_filter() -> KeywordFilter:
    return KeywordFilter.from_yaml(KEYWORDS_PATH)


@pytest.mark.parametrize(
    "text",
    [
        "СТЕЛЛАЖ металлический",
        "стелаж архивный",
        "сттеллаж складской",
        "сөре металлическая",
    ],
)
def test_matches(keyword_filter: KeywordFilter, text: str):
    assert keyword_filter.match_text(text) is not None


def test_no_match(keyword_filter: KeywordFilter):
    assert keyword_filter.match_text("обычный телевизор") is None


def test_match_any_checks_all_fields(keyword_filter: KeywordFilter):
    fields = [None, "", "обычный телевизор", "Сейф офисный"]
    assert keyword_filter.match_any(fields) == "сейф"


def test_match_any_returns_none_when_nothing_matches(keyword_filter: KeywordFilter):
    fields = [None, "", "обычный телевизор", "ноутбук"]
    assert keyword_filter.match_any(fields) is None


def test_keywords_loaded_and_deduplicated():
    kf = KeywordFilter(["стеллаж", "Стеллаж", "СТЕЛЛАЖ", "полка"])
    # exact (post-normalization) duplicates collapse to one entry
    assert len(kf.keywords) == 2


def test_all_configured_keywords_present(keyword_filter: KeywordFilter):
    assert "двери дт" in keyword_filter.keywords
    assert "ключница" in keyword_filter.keywords
    assert len(keyword_filter.keywords) >= 85
