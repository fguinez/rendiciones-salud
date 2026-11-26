"""Match incoming transfers to the amounts each insurer approved.

An insurer may pay several approved rendiciones with one transfer, so a transfer
confirms the smallest set of pending rendiciones whose amounts add up to it exactly
(ties go to the oldest). Only transfers whose glosa matches the insurer's configured
text count, and a transfer never pays a rendición submitted after it.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from rendiciones.bank import BankMovement

MAX_CANDIDATES = 16
MAX_GROUP = 6
_SPACES_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Expected:
    """An approved reimbursement still waiting for its transfer."""

    rendicion_id: int
    monto: int
    earliest: date


@dataclass(frozen=True)
class Match:
    """A transfer and the rendiciones it pays."""

    movement: BankMovement
    expected: tuple[Expected, ...]


def _norm(text: str) -> str:
    return _SPACES_RE.sub(" ", text).strip().casefold()


def glosa_matches(description: str, patterns: Iterable[str]) -> bool:
    """True when ``description`` contains any non-empty pattern (case and spacing ignored)."""
    text = _norm(description)
    return any(p and _norm(p) in text for p in patterns)


def _best_group(candidates: Sequence[Expected], amount: int) -> tuple[Expected, ...] | None:
    for size in range(1, min(MAX_GROUP, len(candidates)) + 1):
        for group in itertools.combinations(candidates, size):
            if sum(e.monto for e in group) == amount:
                return group
    return None


def match_transfers(
    expected: Sequence[Expected],
    movements: Sequence[BankMovement],
    patterns: Sequence[str],
    used_keys: frozenset[str] = frozenset(),
) -> list[Match]:
    """Pair each matching transfer (oldest first) with the rendiciones it pays."""
    pending = sorted(expected, key=lambda e: (e.earliest, e.rendicion_id))
    transfers = sorted(
        (m for m in movements if m.key not in used_keys and glosa_matches(m.description, patterns)),
        key=lambda m: (m.occurred_on, m.key),
    )
    matches: list[Match] = []
    for movement in transfers:
        candidates = [e for e in pending if e.earliest <= movement.occurred_on][:MAX_CANDIDATES]
        group = _best_group(candidates, movement.amount)
        if group is None:
            continue
        matches.append(Match(movement, group))
        pending = [e for e in pending if e not in group]
    return matches
