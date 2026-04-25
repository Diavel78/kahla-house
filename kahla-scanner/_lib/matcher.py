"""Team-name matching utilities used by `scrapers/odds_api.py`.

After the divergence/Brier pipeline retirement, only the pure helpers
(team-name canonicalization + fuzzy matching) are needed here. The
cross-venue linking logic has been removed because the scraper reads
all books from a single API payload — no per-source matching.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

# Below this fuzzy score, we refuse to auto-link two events.
FUZZY_THRESHOLD = 88


def _normalize(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    s = re.sub(r"[^\w\s]", " ", name.lower())
    return re.sub(r"\s+", " ", s).strip()


def canonicalize(name: str, aliases: dict[str, str]) -> str:
    """Apply alias map, fall back to normalized form."""
    norm = _normalize(name)
    return aliases.get(norm, norm)


def _teams_key(home: str, away: str, aliases: dict[str, str]) -> frozenset[str]:
    return frozenset({canonicalize(home, aliases), canonicalize(away, aliases)})


def _fuzzy_teams_match(
    a_home: str, a_away: str, b_home: str, b_away: str, aliases: dict[str, str]
) -> int:
    """Max score across the two possible pairings."""
    ah, aa = canonicalize(a_home, aliases), canonicalize(a_away, aliases)
    bh, ba = canonicalize(b_home, aliases), canonicalize(b_away, aliases)
    same = min(fuzz.ratio(ah, bh), fuzz.ratio(aa, ba))
    swap = min(fuzz.ratio(ah, ba), fuzz.ratio(aa, bh))
    return max(same, swap)


def _split_event_name(name: str) -> tuple[str | None, str | None]:
    """event_name convention: 'Away @ Home' or 'A vs B'."""
    for sep in [" @ ", " vs ", " v. ", " vs. "]:
        if sep in name:
            parts = name.split(sep, 1)
            if len(parts) == 2:
                return parts[0].strip(), parts[1].strip()
    return None, None
