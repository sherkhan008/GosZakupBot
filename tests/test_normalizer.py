import unicodedata

from app.filters.text_normalizer import normalize


def test_case_insensitivity():
    assert normalize("СТЕЛЛАЖ") == normalize("Стеллаж") == normalize("стеллаж")


def test_yo_to_ye_replacement():
    assert normalize("сёре") == normalize("сере")
    assert "ё" not in normalize("ёлка стеллаж")


def test_extra_whitespace_collapsed():
    assert normalize("  стеллаж    металлический  \t\n архивный ") == "стеллаж металлический архивный"


def test_unicode_nfkc_normalization():
    # Decompose 'ё' into 'е' + combining diaeresis (U+0308); NFKC should
    # recompose it before our ё->е replacement runs, giving the same result
    # as normalizing the precomposed form directly.
    precomposed = "сёре"
    decomposed = unicodedata.normalize("NFD", precomposed)
    assert decomposed != precomposed  # sanity check the decomposition actually changed it
    assert normalize(decomposed) == normalize(precomposed) == "сере"


def test_punctuation_becomes_space():
    assert normalize("стеллаж, металлический!") == "стеллаж металлический"


def test_none_and_empty():
    assert normalize(None) == ""
    assert normalize("") == ""
    assert normalize("   ") == ""
