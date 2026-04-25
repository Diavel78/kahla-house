"""Odds / probability helpers."""
from __future__ import annotations


def american_to_prob(price: int) -> float:
    """Convert American odds to raw implied probability (still has vig)."""
    if price == 0:
        raise ValueError("American price cannot be 0")
    if price > 0:
        return 100.0 / (price + 100.0)
    return -price / (-price + 100.0)


def prob_to_american(prob: float) -> int:
    """Convert implied prob to American odds (rounded)."""
    if not 0 < prob < 1:
        raise ValueError("prob must be in (0,1)")
    if prob >= 0.5:
        return int(round(-prob / (1 - prob) * 100))
    return int(round((1 - prob) / prob * 100))


def devig_two_way(p_a: float, p_b: float) -> float:
    """Remove proportional vig from a two-way market. Returns no-vig prob of side A."""
    total = p_a + p_b
    if total <= 0:
        raise ValueError("Sum of probs must be > 0")
    return p_a / total


def devig_multiway(probs: list[float]) -> list[float]:
    """Proportional devig across N outcomes."""
    total = sum(probs)
    if total <= 0:
        raise ValueError("Sum of probs must be > 0")
    return [p / total for p in probs]
