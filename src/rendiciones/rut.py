"""Chilean RUT parsing, validation and formatting."""

from __future__ import annotations

import re

_CLEAN_RE = re.compile(r"[^0-9kK]")


def compute_dv(body: int) -> str:
    """Return the check digit (módulo 11) for a RUT body."""
    total = 0
    factor = 2
    for digit in reversed(str(body)):
        total += int(digit) * factor
        factor = 2 if factor == 7 else factor + 1
    rest = 11 - (total % 11)
    if rest == 11:
        return "0"
    if rest == 10:
        return "K"
    return str(rest)


def normalize(raw: str) -> str | None:
    """Return the canonical ``12345678-9`` form, or None if malformed or invalid."""
    cleaned = _CLEAN_RE.sub("", raw).upper()
    if len(cleaned) < 2:
        return None
    body, dv = cleaned[:-1], cleaned[-1]
    if not body.isdigit() or not 1 <= len(body) <= 8:
        return None
    if compute_dv(int(body)) != dv:
        return None
    return f"{int(body)}-{dv}"


def is_valid(raw: str) -> bool:
    """Return True when ``raw`` is a well-formed RUT with a correct check digit."""
    return normalize(raw) is not None


def format_rut(canonical: str) -> str:
    """Format a canonical RUT with thousands separators (``12.345.678-9``)."""
    body, dv = canonical.split("-")
    return f"{int(body):,}".replace(",", ".") + f"-{dv}"
