"""What to look for in the bank: the glosa each insurer's transfer carries, and how long to wait.

Stored as 0600 JSON in the data directory (``bank.json``); editable from Cuentas.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rendiciones.config import FILE_MODE

logger = logging.getLogger(__name__)

BANK_SETTINGS_FILE = "bank.json"
DEFAULT_GLOSAS: dict[str, tuple[str, ...]] = {
    "colmena": ("Pago:proveedores 0762966190",),
    "chubb": ("Pago:proveedores 0774088202",),
}
DEFAULT_WAIT_DAYS = 30
MAX_PATTERNS = 5
MAX_PATTERN_LENGTH = 120


@dataclass(frozen=True)
class BankSettings:
    """Glosas per insurer and the days a transfer may take after approval."""

    glosas: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULT_GLOSAS))
    wait_days: int = DEFAULT_WAIT_DAYS

    def patterns(self, insurer: str) -> tuple[str, ...]:
        """Glosas that identify ``insurer``'s transfers."""
        return self.glosas.get(insurer, ())

    def as_json(self) -> dict[str, Any]:
        """Plain dict for JSON."""
        return {"glosas": {k: list(v) for k, v in self.glosas.items()}, "wait_days": self.wait_days}


def validate(data: dict[str, Any]) -> BankSettings:
    """Settings from user input, or ``ValueError`` with a message safe to show."""
    raw_glosas = data.get("glosas")
    if not isinstance(raw_glosas, dict):
        raise ValueError("Faltan las glosas por aseguradora")
    glosas: dict[str, tuple[str, ...]] = {}
    for insurer in DEFAULT_GLOSAS:
        values = raw_glosas.get(insurer, [])
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            raise ValueError("Las glosas deben ser texto")
        cleaned = tuple(" ".join(str(v).split()) for v in values if str(v).strip())
        if len(cleaned) > MAX_PATTERNS or any(len(v) > MAX_PATTERN_LENGTH for v in cleaned):
            raise ValueError("Demasiadas glosas o glosa demasiado larga")
        glosas[insurer] = cleaned
    wait_days = data.get("wait_days", DEFAULT_WAIT_DAYS)
    if not isinstance(wait_days, int) or isinstance(wait_days, bool) or not 1 <= wait_days <= 365:
        raise ValueError("Los días de espera deben estar entre 1 y 365")
    return BankSettings(glosas=glosas, wait_days=wait_days)


class BankSettingsStore:
    """``bank.json`` in the data directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> BankSettingsStore:
        """Store at ``<data_dir>/bank.json``."""
        return cls(data_dir / BANK_SETTINGS_FILE)

    def read(self) -> BankSettings:
        """Saved settings, or the defaults when missing or unreadable."""
        try:
            return validate(json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return BankSettings()
        except (OSError, ValueError, TypeError):
            logger.warning("saved bank settings are unreadable; using the defaults")
            return BankSettings()

    def save(self, settings: BankSettings) -> BankSettings:
        """Persist ``settings`` atomically."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(settings.as_json(), fh)
            tmp.replace(self.path)
        return settings
