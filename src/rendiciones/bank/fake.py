"""In-memory bank clients for tests and demos."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date

from rendiciones.bank import BankMovement
from rendiciones.bank.expected import expected_transfer
from rendiciones.bank.settings import BankSettings
from rendiciones.db import Repository, utcnow


class FakeBankClient:
    """Returns scripted abonos; ``reads`` counts how often the cartola was read."""

    name = "fake-bank"

    def __init__(self, movements: Sequence[BankMovement] = (), fail: Exception | None = None):
        self.movements = list(movements)
        self.fail = fail
        self.reads = 0

    def credits(self, since: date, wanted: Callable[[str], bool]) -> list[BankMovement]:
        """Scripted abonos on or after ``since`` whose glosa is wanted."""
        self.reads += 1
        if self.fail is not None:
            raise self.fail
        return [m for m in self.movements if m.occurred_on >= since and wanted(m.description)]


class PayingFakeBank(FakeBankClient):
    """Demo bank that has already received every approved reimbursement (one transfer each)."""

    def __init__(self, repo: Repository, settings: BankSettings) -> None:
        super().__init__()
        self.repo = repo
        self.settings = settings

    def credits(self, since: date, wanted: Callable[[str], bool]) -> list[BankMovement]:
        """One abono per approved insurer amount, with that insurer's first glosa."""
        self.movements = []
        today = utcnow().date()
        for rendicion in self.repo.list_rendiciones():
            for insurer in ("colmena", "chubb"):
                monto = expected_transfer(insurer, rendicion)
                glosas = self.settings.patterns(insurer)
                if not monto or not glosas:
                    continue
                key = f"fake:{insurer}:{rendicion.id}"
                self.movements.append(BankMovement(key, key, today, monto, glosas[0]))
        return super().credits(since, wanted)
