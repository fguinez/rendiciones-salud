"""Bank checks: confirm that approved reimbursements actually reach the bank account.

A bank client reads the account's incoming transfers (``BankMovement``); the pipeline
matches them against what each insurer approved (``rendiciones.bank.matching``).
Implementations must not log personal data (account numbers, names, amounts).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

BANK_CREDENTIAL = "bancochile"


class BankError(RuntimeError):
    """A bank read failed; the message is safe to show (no personal data)."""


@dataclass(frozen=True)
class BankMovement:
    """One incoming transfer (abono) on the checking account.

    ``key`` identifies the movement across reads: the bank's composite row id plus the
    running balance (stable across logins and unique), else the operation id. The
    operation id is the portal's "ID Transacción", kept for display.
    """

    key: str
    operation_id: str | None
    occurred_on: date
    amount: int
    description: str


@runtime_checkable
class BankClient(Protocol):
    """Reads incoming transfers from a bank account."""

    name: str

    def credits(self, since: date, wanted: Callable[[str], bool]) -> list[BankMovement]:
        """Abonos on or after ``since`` whose description satisfies ``wanted``."""
        ...
