"""Path normalisation, prefix overlap and gitignore-style glob matching.

Every scope decision in the system (leases, diff scope guard, risk rules) goes
through this module, so "is this path inside that scope" has exactly one answer.
"""

from __future__ import annotations

import fnmatch
import posixpath
from typing import Iterable


def normalize(path: str) -> str:
    """Return a repo-relative POSIX path; the repo root is the empty string."""

    text = (path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    text = text.lstrip("/")
    if text in ("", "."):
        return ""
    text = posixpath.normpath(text)
    if text == ".":
        return ""
    return text.rstrip("/")


def normalize_many(paths: Iterable[str]) -> list[str]:
    """Normalise, de-duplicate and sort (sorted order is also the lock order)."""

    return sorted({normalize(p) for p in paths})


def is_within(path: str, scope: str) -> bool:
    """True when ``path`` is ``scope`` or lives underneath it."""

    path, scope = normalize(path), normalize(scope)
    if scope == "":
        return True
    return path == scope or path.startswith(scope + "/")


def overlaps(a: str, b: str) -> bool:
    return is_within(a, b) or is_within(b, a)


def glob_match(path: str, pattern: str) -> bool:
    """Segment-aware glob: ``*`` stays inside one segment, ``**`` spans segments."""

    path_segments = [s for s in normalize(path).split("/") if s]
    pattern_segments = [s for s in normalize(pattern).split("/") if s]
    return _match_segments(path_segments, pattern_segments)


def _match_segments(segments: list[str], patterns: list[str]) -> bool:
    if not patterns:
        return not segments
    head, rest = patterns[0], patterns[1:]
    if head == "**":
        for index in range(len(segments) + 1):
            if _match_segments(segments[index:], rest):
                return True
        return False
    if not segments:
        return False
    if fnmatch.fnmatchcase(segments[0], head):
        return _match_segments(segments[1:], rest)
    return False


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(glob_match(path, pattern) for pattern in patterns if pattern)
