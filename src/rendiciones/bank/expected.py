"""How much each insurer's transfer should bring for a rendición.

Colmena pays the bonificación plus, when the surplus contributions were used to cover
the copago, the "Uso Excedente" amount, in the same transfer. Its API only reports the
bonificación, so the excedente is read from the comprobante de bonificación.
"""

from __future__ import annotations

import logging
import re

from rendiciones.domain import RendicionView
from rendiciones.recognition.text import pdf_text
from rendiciones.workflow import approved_monto

logger = logging.getLogger(__name__)

_USO_EXCEDENTE_RE = re.compile(r"uso\s+excedentes?\s*\$?\s*([\d.]+)", re.I)


def uso_excedente(text: str) -> int:
    """The "Uso Excedente" amount in a Colmena comprobante's text (0 when absent)."""
    match = _USO_EXCEDENTE_RE.search(text)
    if match is None:
        return 0
    digits = match.group(1).replace(".", "")
    return int(digits) if digits.isdigit() else 0


def colmena_excedente(rendicion: RendicionView) -> int:
    """Excedentes Colmena paid with the bonificación of ``rendicion`` (0 when unknown)."""
    doc = rendicion.bonificacion_document
    if doc is None or doc.mime != "application/pdf":
        return 0
    try:
        return uso_excedente(pdf_text(doc.path))
    except Exception as exc:
        name = type(exc).__name__
        logger.warning("rendicion %d: comprobante unreadable (%s)", rendicion.id, name)
        return 0


def expected_transfer(insurer: str, rendicion: RendicionView) -> int | None:
    """What ``insurer``'s transfer should bring for ``rendicion`` (None until approved)."""
    monto = approved_monto(insurer, rendicion)
    if not monto:
        return monto
    if insurer == "colmena":
        monto += colmena_excedente(rendicion)
    return monto
