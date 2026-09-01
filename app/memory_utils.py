"""Lightweight, dependency-free RSS memory sampling for diagnostic logging.

Deliberately stdlib-only (no psutil, which is not otherwise a project
dependency): reads /proc/self/status, present on every Linux container
(including Railway's) but not on Windows/macOS, where get_rss_mb() simply
returns None rather than raising or requiring a platform-specific extra.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

_PROC_STATUS = Path("/proc/self/status")


def get_rss_mb() -> Optional[float]:
    """Current resident set size (VmRSS) in MiB, or None if unavailable on
    this platform."""
    try:
        text = _PROC_STATUS.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) / 1024.0
    return None
