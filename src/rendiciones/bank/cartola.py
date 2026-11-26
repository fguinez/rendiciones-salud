"""Pure helpers for Banco de Chile's ``movimientos/getCartola`` payloads.

Ported from el-chanchito (``apps/scrapers/scrapers/backends/banchile_movements.py``),
reduced to the CLP checking cartola. Each row has ``monto`` (unsigned, Chilean
format), ``tipo`` ("abono" / "cargo"), ``descripcion`` (the glosa shown in the
portal), ``fecha`` ("YYYYMMDD HH:MM:SS"), ``fechaContable`` ("DD/MM/YYYY"), a composite
``id`` and the running ``saldo``. The operation id ("ID Transacción") is either inline
in ``detalleGlosa`` or one ``cartola/detalle-glosa`` call away (``transaccionId``).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from rendiciones.bank import BankMovement

_GLOSA_ID_RE = re.compile(r"^\s*id\s+transacci[oó]n\s*[:\-]\s*(.+?)\s*$", re.I)
_CARTOLA_FECHA_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})")
_DDMMYYYY_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_ALL_ZEROS_RE = re.compile(r"^0+$")
_ID_NOISE_RE = re.compile(r"[^0-9A-Za-z]")
_SPACES_RE = re.compile(r"\s+")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_clp(raw: Any) -> int | None:
    """Whole pesos from ``"$1.234.567"``, ``"28.000"``, ``"1.234,56"`` or a JSON number."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return round(raw)
    cleaned = re.sub(r"(?i)CLP|\$|\s", "", _text(raw))
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(".", "")
    if not re.search(r"\d", cleaned):
        return None
    try:
        return round(float(cleaned))
    except ValueError:
        return None


def _date_ddmmyyyy(raw: str) -> date | None:
    match = _DDMMYYYY_RE.search(raw)
    if match is None:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def movement_date(raw: dict[str, Any]) -> date | None:
    """When the movement happened (``fecha``), else when it was posted (``fechaContable``)."""
    match = _CARTOLA_FECHA_RE.match(_text(raw.get("fecha")))
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            pass
    return _date_ddmmyyyy(_text(raw.get("fechaContable")))


def operation_id(raw: Any) -> str | None:
    """An operation id, unless blank or an all-zero placeholder."""
    value = _text(raw)
    compact = _ID_NOISE_RE.sub("", value)
    if not compact or _ALL_ZEROS_RE.match(compact):
        return None
    return value


def operation_id_from_glosa(detalle_glosa: Any) -> str | None:
    """The "Id transaccion: ..." value of a row's inline ``detalleGlosa``."""
    if not isinstance(detalle_glosa, list | tuple):
        return None
    for line in detalle_glosa:
        match = _GLOSA_ID_RE.match(line) if isinstance(line, str) else None
        if match:
            value = operation_id(match.group(1))
            if value is not None:
                return value
    return None


def operation_id_from_detail(payload: Any) -> str | None:
    """The ``transaccionId`` of a ``cartola/detalle-glosa`` 200 response."""
    return operation_id(payload.get("transaccionId")) if isinstance(payload, dict) else None


def needs_detail(raw: dict[str, Any]) -> bool:
    """True when the operation id needs the extra ``detalle-glosa`` call."""
    if raw.get("detalleGlosa"):
        return False
    return bool(_text(raw.get("infoDataGlosaAdicional"))) and bool(_text(raw.get("numeroCuenta")))


def detail_body(raw: dict[str, Any]) -> dict[str, str]:
    """Request body of ``cartola/detalle-glosa`` for one row."""
    return {
        "infoDataGlosaAdicional": _text(raw.get("infoDataGlosaAdicional")),
        "numeroCuenta": _text(raw.get("numeroCuenta")),
    }


def rows(payload: Any) -> list[dict[str, Any]]:
    """The movement rows of a cartola payload."""
    movimientos = payload.get("movimientos") if isinstance(payload, dict) else None
    if not isinstance(movimientos, list):
        return []
    return [row for row in movimientos if isinstance(row, dict)]


def is_credit(raw: dict[str, Any]) -> bool:
    """True for an abono (incoming money)."""
    return _text(raw.get("tipo")).lower() == "abono"


def description(raw: dict[str, Any]) -> str:
    """The glosa shown in the portal, whitespace collapsed."""
    return _SPACES_RE.sub(" ", _text(raw.get("descripcion")))


def account_number(payload: Any) -> str:
    """Which account a payload belongs to ("" when unknown); only ever compared."""
    for row in rows(payload):
        number = _text(row.get("numeroCuenta"))
        if number:
            return number
    return ""


def is_clp(payload: Any, request_body: Any = None) -> bool:
    """False when the payload, or the request behind it, names another currency."""
    moneda = _text(payload.get("moneda")).upper() if isinstance(payload, dict) else ""
    if not moneda and isinstance(request_body, dict):
        selected = request_body.get("cuentaSeleccionada")
        if isinstance(selected, dict):
            moneda = _text(selected.get("moneda")).upper()
    return moneda in ("", "CLP")


def parse_credit(raw: dict[str, Any], op_id: str | None = None) -> BankMovement | None:
    """One abono row as a ``BankMovement`` (None for cargos and unusable rows)."""
    if not is_credit(raw):
        return None
    amount = parse_clp(raw.get("monto"))
    occurred = movement_date(raw)
    if amount is None or occurred is None:
        return None
    op_id = op_id or operation_id_from_glosa(raw.get("detalleGlosa"))
    bank_id, saldo = _text(raw.get("id")), _text(str(raw.get("saldo") or ""))
    if bank_id or saldo:
        key = f"fp:{bank_id}|{saldo}"
    elif op_id is not None:
        key = "op:" + _ID_NOISE_RE.sub("", op_id).upper()
    else:
        return None
    return BankMovement(
        key=key,
        operation_id=op_id,
        occurred_on=occurred,
        amount=abs(amount),
        description=description(raw),
    )
