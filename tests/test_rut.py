"""Tests for Chilean RUT handling."""

from __future__ import annotations

import pytest

from rendiciones import rut


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("11.111.111-1", "11111111-1"),
        ("111111111", "11111111-1"),
        ("22.333.444-k", "22333444-K"),
        ("76.123.456-0", "76123456-0"),
        ("  15555555 - 6 ", "15555555-6"),
    ],
)
def test_normalize_valid(raw: str, expected: str) -> None:
    """Valid RUTs in any common format normalise to ``body-DV``."""
    result = rut.normalize(raw)

    assert result == expected


@pytest.mark.parametrize("raw", ["11.111.111-2", "", "K", "abc", "123456789-0", "1-K"])
def test_normalize_invalid(raw: str) -> None:
    """Malformed RUTs or wrong check digits are rejected."""
    result = rut.normalize(raw)

    assert result is None
    assert not rut.is_valid(raw)


@pytest.mark.parametrize(
    ("body", "dv"), [(11111111, "1"), (22333444, "K"), (76123456, "0"), (15555555, "6")]
)
def test_compute_dv(body: int, dv: str) -> None:
    """The módulo-11 check digit matches known values."""
    result = rut.compute_dv(body)

    assert result == dv


def test_format_rut() -> None:
    """Canonical RUTs are formatted with thousands separators."""
    result = rut.format_rut("22333444-K")

    assert result == "22.333.444-K"
