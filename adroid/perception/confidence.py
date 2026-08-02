"""Confidence scoring for UI element matching.

Heuristic scoring system (not ML — v0.3.0 scope is deterministic).
Each selector strategy gets a base confidence score. The score is
adjusted by the runtime based on historical success rate.

Scoring table (research-verified heuristics from self-healing
test automation literature, Selenium/Appium fallback patterns):

  Strategy                    | Base Score | Rationale
  ----------------------------|------------|----------
  text (exact match)          | 1.00       | Most specific, unambiguous
  resource_id (exact)         | 0.90       | Stable across UI changes
  description (content-desc)  | 0.85       | Usually set by devs, stable
  text_contains (substring)   | 0.80       | May match multiple elements
  class_name                  | 0.50       | Low specificity, many matches
  resource_id (suffix match)  | 0.75       | May match wrong element
  visible bounds only         | 0.30       | Last resort, fragile
  vision (VLM)                | 0.20       | Reserved for v0.4.0

Historical adjustment:
  If a strategy has succeeded >5 times for the same app+action,
  boost its score by +0.05 (max 1.0).
  If a strategy has failed >3 times, reduce its score by -0.15 (min 0.0).

The final confidence is: base_score + historical_adjustment, clamped [0, 1].
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SelectorStrategy(str, Enum):
    """Closed set of selector matching strategies, ordered by specificity.

    The order matters for self-healing fallback: the runtime tries
    strategies in descending confidence order until one matches.
    """

    TEXT_EXACT = "text_exact"           # Selector.text == node.text
    RESOURCE_ID = "resource_id"          # Selector.resource_id matches
    DESCRIPTION = "description"          # Selector.description == node.description
    TEXT_CONTAINS = "text_contains"      # Selector.text_contains in node.text
    RESOURCE_ID_SUFFIX = "resource_id_suffix"  # Suffix match on resource_id
    CLASS_NAME = "class_name"            # Selector.class_name == node.class_name
    BOUNDS_ONLY = "bounds_only"          # No semantic match, just bounds
    VISION = "vision"                    # VLM-based detection (v0.4.0)


# Base confidence scores per strategy
BASE_SCORES: dict[SelectorStrategy, float] = {
    SelectorStrategy.TEXT_EXACT: 1.00,
    SelectorStrategy.RESOURCE_ID: 0.90,
    SelectorStrategy.DESCRIPTION: 0.85,
    SelectorStrategy.TEXT_CONTAINS: 0.80,
    SelectorStrategy.RESOURCE_ID_SUFFIX: 0.75,
    SelectorStrategy.CLASS_NAME: 0.50,
    SelectorStrategy.BOUNDS_ONLY: 0.30,
    SelectorStrategy.VISION: 0.20,
}


@dataclass
class StrategyStats:
    """Rolling statistics for a selector strategy per app+action.

    Tracks success/failure counts to adjust confidence over time.
    The adjustment formula is deliberately simple (no ML, no RL):
        adjustment = min(+0.05, success_count * 0.01) - min(+0.15, failure_count * 0.05)
    This gives a gentle boost for consistently-successful strategies
    and a stronger penalty for consistently-failing ones.
    """

    success_count: int = 0
    failure_count: int = 0

    @property
    def total(self) -> int:
        return self.success_count + self.failure_count

    @property
    def success_rate(self) -> float:
        """0.0-1.0 success rate. Returns 0.5 for no data (neutral prior)."""
        if self.total == 0:
            return 0.5
        return self.success_count / self.total

    @property
    def adjustment(self) -> float:
        """Confidence adjustment based on history. Range: -0.15 to +0.05."""
        boost = min(0.05, self.success_count * 0.01)
        penalty = min(0.15, self.failure_count * 0.05)
        return boost - penalty

    def record_success(self) -> None:
        self.success_count += 1

    def record_failure(self) -> None:
        self.failure_count += 1


class ConfidenceTracker:
    """Tracks per-strategy statistics and computes adjusted confidence.

    One instance per runtime. The tracker is in-memory only (not persisted)
    for v0.3.0. v0.4.0+ may persist stats to skill_memory for cross-session
    learning.

    Usage:
        tracker = ConfidenceTracker()
        score = tracker.get_confidence(SelectorStrategy.TEXT_EXACT,
                                        app="com.youtube", action="search_tap")
        # ... try the strategy ...
        if matched:
            tracker.record_success(SelectorStrategy.TEXT_EXACT,
                                   app="com.youtube", action="search_tap")
        else:
            tracker.record_failure(...)
    """

    def __init__(self) -> None:
        # Key: (app, action, strategy) → StrategyStats
        self._stats: dict[tuple[str, str, SelectorStrategy], StrategyStats] = {}

    def get_confidence(
        self,
        strategy: SelectorStrategy,
        *,
        app: str = "",
        action: str = "",
    ) -> float:
        """Get adjusted confidence for a strategy.

        Returns base_score + historical_adjustment, clamped [0, 1].
        """
        base = BASE_SCORES.get(strategy, 0.3)
        key = (app, action, strategy)
        stats = self._stats.get(key)
        if stats is None:
            return base
        adjusted = base + stats.adjustment
        return max(0.0, min(1.0, adjusted))

    def get_stats(
        self,
        strategy: SelectorStrategy,
        *,
        app: str = "",
        action: str = "",
    ) -> StrategyStats:
        """Get raw stats for a strategy (for inspection)."""
        key = (app, action, strategy)
        if key not in self._stats:
            self._stats[key] = StrategyStats()
        return self._stats[key]

    def record_success(
        self,
        strategy: SelectorStrategy,
        *,
        app: str = "",
        action: str = "",
    ) -> None:
        """Record a successful match with this strategy."""
        key = (app, action, strategy)
        if key not in self._stats:
            self._stats[key] = StrategyStats()
        self._stats[key].record_success()

    def record_failure(
        self,
        strategy: SelectorStrategy,
        *,
        app: str = "",
        action: str = "",
    ) -> None:
        """Record a failed match with this strategy."""
        key = (app, action, strategy)
        if key not in self._stats:
            self._stats[key] = StrategyStats()
        self._stats[key].record_failure()

    def get_fallback_order(
        self,
        *,
        app: str = "",
        action: str = "",
    ) -> list[SelectorStrategy]:
        """Return strategies sorted by adjusted confidence (descending).

        This is the order the self-healing selector tries them in.
        """
        strategies = list(SelectorStrategy)
        scored = [
            (s, self.get_confidence(s, app=app, action=action))
            for s in strategies
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in scored]


__all__ = [
    "SelectorStrategy",
    "BASE_SCORES",
    "StrategyStats",
    "ConfidenceTracker",
]
