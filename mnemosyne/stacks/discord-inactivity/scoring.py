"""Incremental exponential decay.

Mathematically equivalent to summing every event with a decay factor, but it
needs two columns instead of an event log. Storing every single message would
be the largest data set in the whole system and contradicts data minimisation.
"""

from __future__ import annotations

SECONDS_PER_DAY = 86400


def decayed(score: float, updated_at: int | None, now: int, half_life_days: int) -> float:
    """Decay a stored score forward to `now` without writing it back."""
    if not score or updated_at is None:
        return 0.0
    elapsed_days = (now - updated_at) / SECONDS_PER_DAY
    if elapsed_days <= 0:
        return score
    return score * 0.5 ** (elapsed_days / half_life_days)


def bumped(
    score: float,
    updated_at: int | None,
    now: int,
    half_life_days: int,
    weight: float,
) -> float:
    """Apply one event. Decay first, then add - the order matters."""
    return decayed(score, updated_at, now, half_life_days) + weight


def effective_inactive_seconds(last_activity_at: int, now: int, downtime_seconds: int) -> int:
    """Inactivity with gateway downtime removed.

    Without this, a week of bot downtime makes every member look inactive at
    once. Absence of events is not evidence of absence of people.
    """
    return max(0, (now - last_activity_at) - downtime_seconds)


def stage_for(inactive_days: float, stage_1: int, stage_2: int, stage_3: int) -> int:
    if inactive_days >= stage_3:
        return 3
    if inactive_days >= stage_2:
        return 2
    if inactive_days >= stage_1:
        return 1
    return 0
