"""SQLite persistence (stdlib sqlite3, parametrised queries only)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from rendiciones.config import FILE_MODE
from rendiciones.domain import (
    BankPaymentView,
    DocRole,
    DocType,
    DocumentEdit,
    DocumentView,
    Event,
    Extraction,
    RendicionView,
    Status,
    TipoPrestacion,
    check_transition,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS rendiciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'borrador',
    tipo_prestacion TEXT,
    fecha_atencion TEXT,
    monto INTEGER,
    grouping_confidence REAL NOT NULL DEFAULT 0,
    grouping_reason TEXT NOT NULL DEFAULT '',
    fields_locked INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    status_before_error TEXT,
    colmena_request_id TEXT,
    colmena_result_json TEXT,
    colmena_monto_bonificado INTEGER,
    bonificacion_document_id INTEGER,
    chubb_request_id TEXT,
    chubb_result_json TEXT,
    chubb_monto_reembolsado INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    colmena_submitted_at TEXT,
    colmena_resolved_at TEXT,
    chubb_submitted_at TEXT,
    chubb_resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rendicion_id INTEGER REFERENCES rendiciones(id) ON DELETE SET NULL,
    batch_id TEXT,
    sha256 TEXT NOT NULL UNIQUE,
    original_name TEXT NOT NULL,
    folder TEXT NOT NULL DEFAULT '',
    stored_path TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'soporte',
    doc_type TEXT NOT NULL DEFAULT 'otro',
    provider_name TEXT,
    provider_rut TEXT,
    patient_rut TEXT,
    fecha TEXT,
    monto INTEGER,
    folio TEXT,
    tipo_prestacion TEXT,
    confidence REAL NOT NULL DEFAULT 0,
    classifier TEXT NOT NULL DEFAULT 'rules',
    extras_json TEXT NOT NULL DEFAULT '{}',
    edited_fields_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_rendicion ON documents(rendicion_id);
CREATE INDEX IF NOT EXISTS idx_documents_batch ON documents(batch_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rendicion_id INTEGER NOT NULL REFERENCES rendiciones(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_rendicion ON events(rendicion_id);
CREATE TABLE IF NOT EXISTS submission_markers (
    rendicion_id INTEGER NOT NULL REFERENCES rendiciones(id) ON DELETE CASCADE,
    portal TEXT NOT NULL,
    state TEXT NOT NULL,
    request_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (rendicion_id, portal)
);
CREATE TABLE IF NOT EXISTS bank_payments (
    rendicion_id INTEGER NOT NULL REFERENCES rendiciones(id) ON DELETE CASCADE,
    account TEXT NOT NULL,
    state TEXT NOT NULL,
    monto INTEGER NOT NULL,
    movement_key TEXT,
    operation_id TEXT,
    movement_amount INTEGER,
    movement_date TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (rendicion_id, account)
);
"""

# Columns added after SCHEMA was first deployed; ALTERed into databases that predate them.
_ADDED_COLUMNS = (("documents", "edited_fields_json", "TEXT NOT NULL DEFAULT '[]'"),)

_EDITABLE_FIELDS = ("tipo_prestacion", "fecha_atencion", "monto")
_RESULT_FIELDS = frozenset(
    {
        "colmena_request_id",
        "colmena_result_json",
        "colmena_monto_bonificado",
        "bonificacion_document_id",
        "chubb_request_id",
        "chubb_result_json",
        "chubb_monto_reembolsado",
        "colmena_submitted_at",
        "colmena_resolved_at",
        "chubb_submitted_at",
        "chubb_resolved_at",
        "error_message",
    }
)


@dataclass(frozen=True)
class SubmissionMarker:
    """Idempotency marker written by portal clients right before the irreversible click."""

    rendicion_id: int
    portal: str
    state: str  # "final_click" (outcome unknown) | "done" (request id captured)
    request_id: str | None
    created_at: datetime
    updated_at: datetime


class NotFoundError(LookupError):
    """Raised when a rendición or document does not exist."""


def utcnow() -> datetime:
    """Current UTC time (seconds precision)."""
    return datetime.now(UTC).replace(microsecond=0)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _json(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else None


class Repository:
    """Data access for rendiciones, documents and events."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._add_missing_columns(conn)
        db_path.chmod(FILE_MODE)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived connection inside a transaction."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---------------------------------------------------------------- documents

    def find_document_by_sha(self, sha256: str) -> DocumentView | None:
        """Return the document with this digest, if already stored."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM documents WHERE sha256 = ?", (sha256,)).fetchone()
        return self._doc(row) if row else None

    def add_document(
        self,
        *,
        sha256: str,
        original_name: str,
        folder: str,
        path: Path,
        mime: str,
        size: int,
        extraction: Extraction,
        batch_id: str | None = None,
        rendicion_id: int | None = None,
        role: DocRole = DocRole.SOPORTE,
    ) -> int:
        """Insert a document and return its id."""
        ex = extraction
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO documents (rendicion_id, batch_id, sha256, original_name, folder,
                    stored_path, mime, size, role, doc_type, provider_name, provider_rut,
                    patient_rut, fecha, monto, folio, tipo_prestacion, confidence, classifier,
                    extras_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rendicion_id,
                    batch_id,
                    sha256,
                    original_name,
                    folder,
                    str(path),
                    mime,
                    size,
                    role.value,
                    ex.doc_type.value,
                    ex.provider_name,
                    ex.provider_rut,
                    ex.patient_rut,
                    ex.fecha.isoformat() if ex.fecha else None,
                    ex.monto,
                    ex.folio,
                    ex.tipo_prestacion.value if ex.tipo_prestacion else None,
                    ex.confidence,
                    ex.classifier,
                    json.dumps(ex.extras, default=str),
                    utcnow().isoformat(),
                ),
            )
            return int(cur.lastrowid or 0)

    def get_document(self, doc_id: int) -> DocumentView:
        """Return one document or raise NotFoundError."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"documento {doc_id}")
        return self._doc(row)

    def ungrouped_documents(self, batch_id: str | None = None) -> list[DocumentView]:
        """Documents not yet assigned to a rendición (optionally of one batch)."""
        query = "SELECT * FROM documents WHERE rendicion_id IS NULL AND role = 'soporte'"
        params: tuple[Any, ...] = ()
        if batch_id is not None:
            query += " AND batch_id = ?"
            params = (batch_id,)
        with self.connect() as conn:
            rows = conn.execute(query + " ORDER BY id", params).fetchall()
        return [self._doc(r) for r in rows]

    def documents_of(self, rendicion_ids: Sequence[int]) -> list[DocumentView]:
        """All documents belonging to the given rendiciones."""
        if not rendicion_ids:
            return []
        marks = ",".join("?" for _ in rendicion_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM documents WHERE rendicion_id IN ({marks}) ORDER BY id",  # noqa: S608
                tuple(rendicion_ids),
            ).fetchall()
        return [self._doc(r) for r in rows]

    def set_document_rendicion(self, doc_id: int, rendicion_id: int | None) -> None:
        """Assign a document to a rendición (or detach it)."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE documents SET rendicion_id = ? WHERE id = ?", (rendicion_id, doc_id)
            )

    def update_document_fields(self, doc_id: int, edit: DocumentEdit) -> None:
        """Overwrite extraction fields with user values and mark them as edited."""
        if not edit.sent:
            return
        columns = sorted(edit.sent)
        assignments = ", ".join(f"{c} = ?" for c in columns)
        params = [self._sql_value(getattr(edit, c)) for c in columns]
        with self.connect() as conn:
            row = conn.execute(
                "SELECT edited_fields_json FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"documento {doc_id}")
            edited = sorted(set(json.loads(row["edited_fields_json"])) | edit.sent)
            conn.execute(
                f"UPDATE documents SET {assignments}, edited_fields_json = ? WHERE id = ?",  # noqa: S608
                (*params, json.dumps(edited), doc_id),
            )

    def set_document_role(self, doc_id: int, role: DocRole) -> None:
        """Change why a document is attached."""
        with self.connect() as conn:
            conn.execute("UPDATE documents SET role = ? WHERE id = ?", (role.value, doc_id))

    def delete_document(self, doc_id: int) -> DocumentView:
        """Delete a document row and return what was deleted."""
        doc = self.get_document(doc_id)
        with self.connect() as conn:
            conn.execute(
                "UPDATE rendiciones SET bonificacion_document_id = NULL "
                "WHERE bonificacion_document_id = ?",
                (doc_id,),
            )
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        return doc

    # --------------------------------------------------------------- rendiciones

    def create_rendicion(
        self,
        *,
        confidence: float,
        reason: str,
        tipo_prestacion: TipoPrestacion | None = None,
        fecha_atencion: date | None = None,
        monto: int | None = None,
    ) -> int:
        """Create a rendición in ``borrador`` and log a creation event."""
        now = utcnow().isoformat()
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO rendiciones (status, tipo_prestacion, fecha_atencion, monto,
                    grouping_confidence, grouping_reason, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    Status.BORRADOR.value,
                    tipo_prestacion.value if tipo_prestacion else None,
                    fecha_atencion.isoformat() if fecha_atencion else None,
                    monto,
                    confidence,
                    reason,
                    now,
                    now,
                ),
            )
            rid = int(cur.lastrowid or 0)
            self._event(conn, rid, "creada", None, Status.BORRADOR, {"reason": reason})
        return rid

    def get_rendicion(self, rendicion_id: int) -> RendicionView:
        """Return one rendición with its documents or raise NotFoundError."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM rendiciones WHERE id = ?", (rendicion_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"rendición {rendicion_id}")
            docs = conn.execute(
                "SELECT * FROM documents WHERE rendicion_id = ? ORDER BY id", (rendicion_id,)
            ).fetchall()
            payments = conn.execute(
                "SELECT * FROM bank_payments WHERE rendicion_id = ?", (rendicion_id,)
            ).fetchall()
        return self._rendicion(
            row, [self._doc(d) for d in docs], {p["account"]: self._payment(p) for p in payments}
        )

    def list_rendiciones(self, status: Status | None = None) -> list[RendicionView]:
        """List rendiciones (newest first) with their documents."""
        with self.connect() as conn:
            if status is None:
                rows = conn.execute("SELECT * FROM rendiciones ORDER BY id DESC").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM rendiciones WHERE status = ? ORDER BY id", (status.value,)
                ).fetchall()
            docs = conn.execute(
                "SELECT * FROM documents WHERE rendicion_id IS NOT NULL ORDER BY id"
            ).fetchall()
            payments = conn.execute("SELECT * FROM bank_payments").fetchall()
        by_rid: dict[int, list[DocumentView]] = {}
        for d in docs:
            by_rid.setdefault(int(d["rendicion_id"]), []).append(self._doc(d))
        paid: dict[int, dict[str, BankPaymentView]] = {}
        for p in payments:
            paid.setdefault(int(p["rendicion_id"]), {})[p["account"]] = self._payment(p)
        return [
            self._rendicion(r, by_rid.get(int(r["id"]), []), paid.get(int(r["id"]), {}))
            for r in rows
        ]

    def count_by_status(self) -> dict[str, int]:
        """Number of rendiciones per status."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM rendiciones GROUP BY status"
            ).fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}

    def update_fields(
        self,
        rendicion_id: int,
        values: dict[str, Any],
        *,
        lock: bool,
        event_kind: str = "editada",
    ) -> None:
        """Update tipo_prestacion / fecha_atencion / monto."""
        unknown = set(values) - set(_EDITABLE_FIELDS)
        if unknown:
            raise ValueError(f"campos no editables: {sorted(unknown)}")
        if not values:
            return
        assignments = ", ".join(f"{k} = ?" for k in values)
        params = [self._sql_value(v) for v in values.values()]
        with self.connect() as conn:
            self._require(conn, rendicion_id)
            conn.execute(
                f"UPDATE rendiciones SET {assignments}, fields_locked = MAX(fields_locked, ?), "  # noqa: S608
                "updated_at = ? WHERE id = ?",
                (*params, 1 if lock else 0, utcnow().isoformat(), rendicion_id),
            )
            if lock:
                self._event(conn, rendicion_id, event_kind, None, None, {"fields": sorted(values)})

    def unlock_fields(self, rendicion_id: int) -> None:
        """Let tipo_prestacion / fecha_atencion / monto follow the documents again."""
        with self.connect() as conn:
            self._require(conn, rendicion_id)
            conn.execute(
                "UPDATE rendiciones SET fields_locked = 0, updated_at = ? WHERE id = ?",
                (utcnow().isoformat(), rendicion_id),
            )
            self._event(conn, rendicion_id, "valores_desbloqueados", None, None, {})

    def set_grouping(self, rendicion_id: int, confidence: float, reason: str) -> None:
        """Update the grouping confidence and reason."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE rendiciones SET grouping_confidence = ?, grouping_reason = ?, "
                "updated_at = ? WHERE id = ?",
                (confidence, reason, utcnow().isoformat(), rendicion_id),
            )

    def transition(
        self,
        rendicion_id: int,
        target: Status,
        *,
        kind: str = "estado",
        detail: dict[str, Any] | None = None,
        updates: dict[str, Any] | None = None,
    ) -> RendicionView:
        """Validate and apply a status change, plus optional result fields, atomically."""
        updates = dict(updates or {})
        unknown = set(updates) - _RESULT_FIELDS
        if unknown:
            raise ValueError(f"campos desconocidos: {sorted(unknown)}")
        with self.connect() as conn:
            row = self._require(conn, rendicion_id)
            current = Status(row["status"])
            previous = Status(row["status_before_error"]) if row["status_before_error"] else None
            check_transition(current, target, previous)
            if target == Status.ERROR:
                updates["status_before_error"] = current.value
            elif current == Status.ERROR:
                updates["status_before_error"] = None
                updates.setdefault("error_message", None)
            updates["status"] = target.value
            updates["updated_at"] = utcnow().isoformat()
            assignments = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE rendiciones SET {assignments} WHERE id = ?",  # noqa: S608
                (*[self._sql_value(v) for v in updates.values()], rendicion_id),
            )
            self._event(conn, rendicion_id, kind, current, target, detail or {})
        return self.get_rendicion(rendicion_id)

    def record_fields(
        self, rendicion_id: int, updates: dict[str, Any], *, kind: str, detail: dict[str, Any]
    ) -> None:
        """Store result fields without a status change (e.g. attaching a receipt)."""
        unknown = set(updates) - _RESULT_FIELDS
        if unknown:
            raise ValueError(f"campos desconocidos: {sorted(unknown)}")
        updates = {**updates, "updated_at": utcnow().isoformat()}
        assignments = ", ".join(f"{k} = ?" for k in updates)
        with self.connect() as conn:
            self._require(conn, rendicion_id)
            conn.execute(
                f"UPDATE rendiciones SET {assignments} WHERE id = ?",  # noqa: S608
                (*[self._sql_value(v) for v in updates.values()], rendicion_id),
            )
            self._event(conn, rendicion_id, kind, None, None, detail)

    def add_event(self, rendicion_id: int, kind: str, detail: dict[str, Any]) -> None:
        """Append a free-form event."""
        with self.connect() as conn:
            self._event(conn, rendicion_id, kind, None, None, detail)

    def delete_rendicion(self, rendicion_id: int) -> list[DocumentView]:
        """Delete a rendición and its documents; return the removed documents."""
        with self.connect() as conn:
            self._require(conn, rendicion_id)
            docs = [
                self._doc(r)
                for r in conn.execute(
                    "SELECT * FROM documents WHERE rendicion_id = ?", (rendicion_id,)
                ).fetchall()
            ]
            conn.execute("DELETE FROM documents WHERE rendicion_id = ?", (rendicion_id,))
            conn.execute("DELETE FROM rendiciones WHERE id = ?", (rendicion_id,))
        return docs

    def events(self, rendicion_id: int) -> list[Event]:
        """History of a rendición, oldest first."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE rendicion_id = ? ORDER BY id", (rendicion_id,)
            ).fetchall()
        return [
            Event(
                id=int(r["id"]),
                rendicion_id=int(r["rendicion_id"]),
                at=datetime.fromisoformat(r["at"]),
                kind=str(r["kind"]),
                from_status=Status(r["from_status"]) if r["from_status"] else None,
                to_status=Status(r["to_status"]) if r["to_status"] else None,
                detail=json.loads(r["detail_json"]),
            )
            for r in rows
        ]

    # ----------------------------------------------------------- submission markers

    def get_marker(self, rendicion_id: int, portal: str) -> SubmissionMarker | None:
        """Return the submission marker of ``rendicion_id`` on ``portal``, if any."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM submission_markers WHERE rendicion_id = ? AND portal = ?",
                (rendicion_id, portal),
            ).fetchone()
        if row is None:
            return None
        return SubmissionMarker(
            rendicion_id=int(row["rendicion_id"]),
            portal=str(row["portal"]),
            state=str(row["state"]),
            request_id=row["request_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def set_marker(
        self, rendicion_id: int, portal: str, state: str, request_id: str | None = None
    ) -> None:
        """Create or update the submission marker (committed before returning)."""
        now = utcnow().isoformat()
        with self.connect() as conn:
            self._require(conn, rendicion_id)
            conn.execute(
                "INSERT INTO submission_markers "
                "(rendicion_id, portal, state, request_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(rendicion_id, portal) DO UPDATE SET state = excluded.state, "
                "request_id = COALESCE(excluded.request_id, request_id), "
                "updated_at = excluded.updated_at",
                (rendicion_id, portal, state, request_id, now, now),
            )
            self._event(conn, rendicion_id, f"{portal}_marcador", None, None, {"state": state})

    def clear_marker(self, rendicion_id: int, portal: str) -> bool:
        """Delete the marker; return True if one existed."""
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM submission_markers WHERE rendicion_id = ? AND portal = ?",
                (rendicion_id, portal),
            )
        return cur.rowcount > 0

    def known_request_ids(self, portal: str) -> set[str]:
        """Request ids already linked to some rendición on ``portal``."""
        column = {"colmena": "colmena_request_id", "chubb": "chubb_request_id"}[portal]
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT {column} AS rid FROM rendiciones WHERE {column} IS NOT NULL"  # noqa: S608
            ).fetchall()
            markers = conn.execute(
                "SELECT request_id FROM submission_markers "
                "WHERE portal = ? AND request_id IS NOT NULL",
                (portal,),
            ).fetchall()
        return {str(r["rid"]) for r in rows} | {str(r["request_id"]) for r in markers}

    # -------------------------------------------------------------- bank payments

    def set_bank_payment(
        self,
        rendicion_id: int,
        account: str,
        state: str,
        monto: int,
        *,
        movement_key: str | None = None,
        operation_id: str | None = None,
        movement_amount: int | None = None,
        movement_date: date | None = None,
        excedente: int = 0,
    ) -> None:
        """Record what the bank check found for ``account`` (an ``abono_*`` node).

        ``excedente`` is the part of ``monto`` paid from the surplus contributions.
        """
        now = utcnow().isoformat()
        with self.connect() as conn:
            self._require(conn, rendicion_id)
            conn.execute(
                "INSERT INTO bank_payments (rendicion_id, account, state, monto, movement_key, "
                "operation_id, movement_amount, movement_date, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(rendicion_id, account) DO UPDATE SET state = excluded.state, "
                "monto = excluded.monto, movement_key = excluded.movement_key, "
                "operation_id = excluded.operation_id, "
                "movement_amount = excluded.movement_amount, "
                "movement_date = excluded.movement_date, updated_at = excluded.updated_at",
                (
                    rendicion_id,
                    account,
                    state,
                    monto,
                    movement_key,
                    operation_id,
                    movement_amount,
                    movement_date.isoformat() if movement_date else None,
                    now,
                ),
            )
            detail: dict[str, Any] = {"account": account, "operation_id": operation_id}
            if excedente:
                detail["monto"] = monto
                detail["excedente"] = excedente
            self._event(conn, rendicion_id, f"abono_{state}", None, None, detail)

    def used_movement_keys(self) -> frozenset[str]:
        """Transfers already linked to some rendición."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT movement_key FROM bank_payments WHERE movement_key IS NOT NULL"
            ).fetchall()
        return frozenset(str(r["movement_key"]) for r in rows)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _sql_value(value: Any) -> Any:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if hasattr(value, "value") and isinstance(value.value, str):
            return value.value
        if isinstance(value, dict):
            return json.dumps(value, default=str)
        return value

    @staticmethod
    def _add_missing_columns(conn: sqlite3.Connection) -> None:
        for table, column, declaration in _ADDED_COLUMNS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @staticmethod
    def _require(conn: sqlite3.Connection, rendicion_id: int) -> sqlite3.Row:
        row: sqlite3.Row | None = conn.execute(
            "SELECT * FROM rendiciones WHERE id = ?", (rendicion_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"rendición {rendicion_id}")
        return row

    @staticmethod
    def _event(
        conn: sqlite3.Connection,
        rendicion_id: int,
        kind: str,
        from_status: Status | None,
        to_status: Status | None,
        detail: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO events (rendicion_id, at, kind, from_status, to_status, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                rendicion_id,
                utcnow().isoformat(),
                kind,
                from_status.value if from_status else None,
                to_status.value if to_status else None,
                json.dumps(detail, default=str),
            ),
        )

    @staticmethod
    def _doc(row: sqlite3.Row) -> DocumentView:
        extraction = Extraction(
            doc_type=DocType(row["doc_type"]),
            provider_name=row["provider_name"],
            provider_rut=row["provider_rut"],
            patient_rut=row["patient_rut"],
            fecha=_d(row["fecha"]),
            monto=row["monto"],
            folio=row["folio"],
            tipo_prestacion=TipoPrestacion(row["tipo_prestacion"])
            if row["tipo_prestacion"]
            else None,
            confidence=float(row["confidence"]),
            classifier=str(row["classifier"]),
            extras=json.loads(row["extras_json"]),
        )
        return DocumentView(
            id=int(row["id"]),
            rendicion_id=row["rendicion_id"],
            sha256=str(row["sha256"]),
            original_name=str(row["original_name"]),
            folder=str(row["folder"]),
            path=Path(row["stored_path"]),
            mime=str(row["mime"]),
            size=int(row["size"]),
            role=DocRole(row["role"]),
            extraction=extraction,
            created_at=datetime.fromisoformat(row["created_at"]),
            edited_fields=frozenset(json.loads(row["edited_fields_json"])),
        )

    @staticmethod
    def _payment(row: sqlite3.Row) -> BankPaymentView:
        return BankPaymentView(
            account=str(row["account"]),
            state=str(row["state"]),
            monto=int(row["monto"]),
            movement_key=row["movement_key"],
            operation_id=row["operation_id"],
            movement_amount=row["movement_amount"],
            movement_date=_d(row["movement_date"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _rendicion(
        row: sqlite3.Row,
        docs: list[DocumentView],
        payments: dict[str, BankPaymentView] | None = None,
    ) -> RendicionView:
        return RendicionView(
            id=int(row["id"]),
            status=Status(row["status"]),
            tipo_prestacion=TipoPrestacion(row["tipo_prestacion"])
            if row["tipo_prestacion"]
            else None,
            fecha_atencion=_d(row["fecha_atencion"]),
            monto=row["monto"],
            grouping_confidence=float(row["grouping_confidence"]),
            grouping_reason=str(row["grouping_reason"]),
            fields_locked=bool(row["fields_locked"]),
            error_message=row["error_message"],
            status_before_error=Status(row["status_before_error"])
            if row["status_before_error"]
            else None,
            colmena_request_id=row["colmena_request_id"],
            colmena_result=_json(row["colmena_result_json"]),
            colmena_monto_bonificado=row["colmena_monto_bonificado"],
            bonificacion_document_id=row["bonificacion_document_id"],
            chubb_request_id=row["chubb_request_id"],
            chubb_result=_json(row["chubb_result_json"]),
            chubb_monto_reembolsado=row["chubb_monto_reembolsado"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            colmena_submitted_at=_dt(row["colmena_submitted_at"]),
            colmena_resolved_at=_dt(row["colmena_resolved_at"]),
            chubb_submitted_at=_dt(row["chubb_submitted_at"]),
            chubb_resolved_at=_dt(row["chubb_resolved_at"]),
            documents=tuple(docs),
            bank_payments=payments or {},
        )


def extraction_to_dict(ex: Extraction) -> dict[str, Any]:
    """JSON-friendly representation of an extraction."""
    data = asdict(ex)
    data["doc_type"] = ex.doc_type.value
    data["tipo_prestacion"] = ex.tipo_prestacion.value if ex.tipo_prestacion else None
    data["fecha"] = ex.fecha.isoformat() if ex.fecha else None
    return data
