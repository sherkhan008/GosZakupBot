"""Text normalization shared by keyword extraction and matching.

Steps (per spec):
1. Unicode NFKC normalization.
2. casefold (stronger than lower() for case-insensitive comparison).
3. Replace 'ё' -> 'е' (and 'Ё' -> 'Е', handled by casefold order below).
4. Replace tabs/newlines with spaces.
5. Convert unnecessary punctuation to spaces where safe.
6. Collapse consecutive whitespace into a single space.
7. Trim leading/trailing whitespace.
"""
from __future__ import annotations

import re
import unicodedata

# Punctuation that is safe to turn into whitespace before matching. Keep this
# conservative: we don't want to merge two different words together, so all of
# these are replaced with a space (not removed outright).
_PUNCTUATION_RE = re.compile(r"[.,;:!?()\[\]{}\"'«»<>@#$%^*_+=|\\/~`]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    if text is None:
        return ""

    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.casefold()
    normalized = normalized.replace("ё", "е")  # ё -> е (casefold already lowercases)
    normalized = normalized.replace("\t", " ").replace("\n", " ").replace("\r", " ")
    normalized = _PUNCTUATION_RE.sub(" ", normalized)
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    return normalized.strip()
