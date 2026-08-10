"""Local keyword matching — the final authority on whether a lot matches.

Server-side GosZakup search filters (nameDescriptionRu/Kz) may be used to narrow
down candidates efficiently, but the actual match/no-match decision is always
made here, against config/keywords.yaml, after text normalization.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

import yaml

from app.filters.text_normalizer import normalize

logger = logging.getLogger(__name__)


class KeywordFilter:
    def __init__(self, keywords: list[str]):
        # Preserve original spelling for logging/storage, keep a parallel
        # normalized list (deduplicated) for matching.
        seen: set[str] = set()
        self._pairs: list[tuple[str, str]] = []  # (normalized, original)
        for kw in keywords:
            norm = normalize(kw)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            self._pairs.append((norm, kw))

    @classmethod
    def from_yaml(cls, path: Path) -> "KeywordFilter":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        keywords = data.get("keywords") or []
        if not keywords:
            logger.warning("No keywords loaded from %s", path)
        return cls(keywords)

    @property
    def keywords(self) -> list[str]:
        return [orig for _, orig in self._pairs]

    def match_text(self, text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        norm_text = normalize(text)
        if not norm_text:
            return None
        for norm_kw, orig_kw in self._pairs:
            if norm_kw in norm_text:
                return orig_kw
        return None

    def match_any(self, fields: Iterable[Optional[str]]) -> Optional[str]:
        for field in fields:
            match = self.match_text(field)
            if match:
                return match
        return None
