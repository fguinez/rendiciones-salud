"""Load portal clients from ``module:function`` specs."""

from __future__ import annotations

import importlib
import os
from typing import Any

from rendiciones.automation.base import ColmenaClient, PortalClient

ENV_VAR = "RENDICIONES_PORTAL_FACTORY"
FAKE_SPEC = "rendiciones.automation.fake:fake_factory"
PLAYWRIGHT_SPEC = "rendiciones.automation.browser:factory"
ALIASES = {"fake": FAKE_SPEC, "playwright": PLAYWRIGHT_SPEC, "browser": PLAYWRIGHT_SPEC}


class PortalConfigError(RuntimeError):
    """Raised when no usable portal clients are configured."""


def resolve_spec(spec: str | None = None) -> str | None:
    """Expand aliases (``fake``, ``playwright``) and fall back to the environment."""
    spec = spec or os.environ.get(ENV_VAR)
    return ALIASES.get(spec, spec) if spec else spec


def load_clients(spec: str | None = None, **options: Any) -> tuple[ColmenaClient, PortalClient]:
    """Import ``module:function`` and call it to obtain ``(colmena, chubb)`` clients.

    ``options`` (e.g. ``dry_run``, ``headed``) are passed to the factory when given.
    """
    spec = resolve_spec(spec)
    if not spec or ":" not in spec:
        raise PortalConfigError(
            f"No hay cliente de portales configurado. Define {ENV_VAR}=modulo:funcion "
            "o usa --clients fake para una demostración."
        )
    module_name, func_name = spec.split(":", 1)
    if not module_name.startswith("rendiciones."):
        raise PortalConfigError("Solo se permiten clientes dentro del paquete rendiciones")
    factory = getattr(importlib.import_module(module_name), func_name)
    colmena, chubb = factory(**options) if options else factory()
    if not isinstance(colmena, ColmenaClient) or not isinstance(chubb, PortalClient):
        raise PortalConfigError("La factoría no devolvió clientes válidos")
    return colmena, chubb
