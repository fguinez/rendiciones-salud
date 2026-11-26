"""Portal credentials kept in the OS keychain (macOS Keychain), never on disk or in logs.

Each portal is one keychain item (service ``rendiciones-salud``, account ``portal:<name>``)
whose secret is a small JSON with the username and password. Only the keychain sees the
password; the API returns at most a masked username.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from keyring.backend import KeyringBackend

logger = logging.getLogger(__name__)

PORTALS = ("colmena", "chubb", "bancochile")
KEYCHAIN_SERVICE = "rendiciones-salud"
MAX_FIELD_LENGTH = 200


class CredentialStoreError(RuntimeError):
    """The keychain is unavailable or refused the operation (message is safe to show)."""


@dataclass(frozen=True)
class Credential:
    """Username and password for one portal. ``repr`` never shows the password."""

    username: str
    password: str = field(repr=False)
    updated_at: str | None = None


class CredentialStore(Protocol):
    """Where credentials live: the OS keychain in production, memory in tests."""

    def get(self, portal: str) -> Credential | None: ...

    def set(self, portal: str, credential: Credential) -> None: ...

    def delete(self, portal: str) -> bool: ...


def check_portal(portal: str) -> str:
    """Return ``portal`` if it is a known portal, else raise ``ValueError``."""
    if portal not in PORTALS:
        raise ValueError(f"portal desconocido: {portal}")
    return portal


def validate(username: str, password: str) -> Credential:
    """Build a credential with trimmed username, rejecting empty or oversized values."""
    username = username.strip()
    if not username or not password:
        raise ValueError("Usuario y contraseña son obligatorios")
    if len(username) > MAX_FIELD_LENGTH or len(password) > MAX_FIELD_LENGTH:
        raise ValueError("Usuario o contraseña demasiado largos")
    return Credential(username, password, datetime.now(UTC).isoformat(timespec="seconds"))


def mask_username(username: str) -> str:
    """Hint that lets the user recognise the account without exposing it fully."""
    if "@" in username:
        local, domain = username.split("@", 1)
        return f"{local[:1]}•••@{domain}"
    if len(username) <= 4:
        return "•" * len(username)
    return "•••" + username[-4:]


def _encode(credential: Credential) -> str:
    return json.dumps(
        {"u": credential.username, "p": credential.password, "t": credential.updated_at}
    )


def _decode(raw: str) -> Credential | None:
    try:
        data = json.loads(raw)
        return Credential(str(data["u"]), str(data["p"]), data.get("t"))
    except (ValueError, KeyError, TypeError):
        logger.warning("ignoring malformed keychain item")
        return None


class KeychainStore:
    """``CredentialStore`` backed by the OS keychain through ``keyring``.

    The backend is chosen explicitly (macOS Keychain on darwin) so a global keyring
    config (e.g. a Google Artifact Registry plugin) or a plaintext fallback is never used.
    """

    def __init__(self, service: str = KEYCHAIN_SERVICE) -> None:
        self.service = service
        self._backend: KeyringBackend | None = None

    def _keyring(self) -> KeyringBackend:
        if self._backend is not None:
            return self._backend
        try:
            if sys.platform == "darwin":
                from keyring.backends import macOS

                self._backend = macOS.Keyring()  # type: ignore[no-untyped-call]
            else:
                import keyring
                from keyring.backends import fail

                backend = keyring.get_keyring()
                module = type(backend).__module__
                if isinstance(backend, fail.Keyring) or "keyrings.alt" in module:
                    raise CredentialStoreError("No hay un llavero seguro disponible en este equipo")
                self._backend = backend
        except ImportError as exc:
            raise CredentialStoreError("Falta la librería keyring") from exc
        return self._backend

    @staticmethod
    def _account(portal: str) -> str:
        return f"portal:{check_portal(portal)}"

    def get(self, portal: str) -> Credential | None:
        """Read the credential for ``portal`` (None if not configured)."""
        try:
            raw = self._keyring().get_password(self.service, self._account(portal))
        except CredentialStoreError:
            raise
        except Exception as exc:
            name = type(exc).__name__
            raise CredentialStoreError(f"No se pudo leer el llavero ({name})") from exc
        return _decode(raw) if raw else None

    def set(self, portal: str, credential: Credential) -> None:
        """Create or replace the credential for ``portal``."""
        try:
            self._keyring().set_password(self.service, self._account(portal), _encode(credential))
        except CredentialStoreError:
            raise
        except Exception as exc:
            raise CredentialStoreError(
                f"No se pudo guardar en el llavero ({type(exc).__name__})"
            ) from exc
        logger.info("credential saved for %s", portal)

    def delete(self, portal: str) -> bool:
        """Remove the credential for ``portal``; False if there was none."""
        if self.get(portal) is None:
            return False
        try:
            self._keyring().delete_password(self.service, self._account(portal))
        except Exception as exc:
            raise CredentialStoreError(
                f"No se pudo borrar del llavero ({type(exc).__name__})"
            ) from exc
        logger.info("credential deleted for %s", portal)
        return True


class MemoryStore:
    """In-memory ``CredentialStore`` for tests and demos."""

    def __init__(self) -> None:
        self._items: dict[str, Credential] = {}

    def get(self, portal: str) -> Credential | None:
        """Stored credential or None."""
        return self._items.get(check_portal(portal))

    def set(self, portal: str, credential: Credential) -> None:
        """Store ``credential``."""
        self._items[check_portal(portal)] = credential

    def delete(self, portal: str) -> bool:
        """Drop the credential; False if there was none."""
        return self._items.pop(check_portal(portal), None) is not None
