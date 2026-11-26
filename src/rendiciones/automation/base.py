"""Interfaces that portal automation (Colmena, Chubb) must implement.

A real implementation (e.g. browser automation) lives next to this file and is loaded
by ``rendiciones.automation.loader`` from ``RENDICIONES_PORTAL_FACTORY=module:function``,
where ``function()`` returns ``(colmena_client, chubb_client)``.

Implementations receive a ``RendicionView`` whose documents carry absolute file paths.
They must not log personal data (names, RUTs, amounts); log ids and counts only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from rendiciones.domain import RendicionView


class PortalError(RuntimeError):
    """A portal failure whose message is safe to show and store (no personal data).

    The pipeline copies ``str(exc)`` into the rendición's ``error_message``; any other
    exception type only contributes its class name.
    """


class SessionExpiredError(PortalError):
    """The browser profile is not logged in to the portal; the user must log in again."""

    def __init__(self, portal: str) -> None:
        super().__init__(
            f"Sesión de {portal} expirada o inexistente: inicia sesión desde Cuentas "
            f"o con `rendiciones login {portal}`"
        )
        self.portal = portal


# Short alias used throughout the docs.
SessionExpired = SessionExpiredError


class PortalState(StrEnum):
    """Outcome of a status check on a portal."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class SubmissionResult:
    """Outcome of submitting a rendición to a portal."""

    ok: bool
    request_id: str | None = None
    message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StatusResult:
    """Outcome of checking a submitted request."""

    state: PortalState
    monto: int | None = None  # monto bonificado (Colmena) or reembolsado (Chubb)
    message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PortalClient(Protocol):
    """Submits rendiciones to an insurer portal and checks their status."""

    name: str

    def submit(self, rendicion: RendicionView) -> SubmissionResult:
        """Submit the rendición (its support documents, plus the bonificación for Chubb)."""
        ...

    def check_status(self, request_id: str) -> StatusResult:
        """Return the current state of a previously submitted request."""
        ...


@runtime_checkable
class ColmenaClient(PortalClient, Protocol):
    """Colmena additionally provides the comprobante de bonificación."""

    def download_bonificacion(self, request_id: str, dest: Path) -> Path:
        """Download the comprobante into directory ``dest`` and return the file path."""
        ...


@runtime_checkable
class LiquidacionProvider(Protocol):
    """A portal that hands out the settlement (liquidación) of a resolved request."""

    def download_liquidacion(self, request_id: str, dest: Path) -> Path | None:
        """Download it into directory ``dest``; None while the portal does not offer it yet."""
        ...
