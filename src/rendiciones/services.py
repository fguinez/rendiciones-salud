"""Application services: ingest uploads, group, edit rendiciones."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import BinaryIO

from rendiciones.config import Settings, prepare_data_dir
from rendiciones.db import Repository
from rendiciones.domain import (
    EDITABLE_STATUSES,
    DocRole,
    DocumentEdit,
    DocumentView,
    RendicionView,
    Status,
    TipoPrestacion,
)
from rendiciones.grouping import GroupItem, group_items, summarize
from rendiciones.recognition.base import Classifier, RecognitionInput
from rendiciones.recognition.text import extract_text
from rendiciones.storage import FileStore, UploadError, sanitize_filename, sanitize_relative_folder

logger = logging.getLogger(__name__)


class NotEditableError(ValueError):
    """Raised when editing a rendición that already left borrador/lista."""


@dataclass
class FileResult:
    """Outcome of ingesting one uploaded file."""

    name: str
    ok: bool
    document_id: int | None = None
    duplicate: bool = False
    error: str | None = None
    doc_type: str | None = None
    rendicion_id: int | None = None


@dataclass
class GroupingResult:
    """Rendiciones created or extended by a grouping run."""

    created: list[int] = field(default_factory=list)
    extended: list[int] = field(default_factory=list)


@dataclass
class MoveResult:
    """Rendición that received the documents and the emptied sources that were deleted."""

    rendicion_id: int
    deleted_rendiciones: list[int] = field(default_factory=list)


def to_item(doc: DocumentView) -> GroupItem:
    """Grouping features of a stored document."""
    ex = doc.extraction
    return GroupItem(
        doc_id=doc.id,
        doc_type=ex.doc_type,
        folder=doc.folder,
        folio=ex.folio,
        provider_rut=ex.provider_rut,
        patient_rut=ex.patient_rut,
        fecha=ex.fecha,
        monto=ex.monto,
        tipo_prestacion=ex.tipo_prestacion,
        rendicion_id=doc.rendicion_id,
    )


@dataclass(frozen=True)
class _Placement:
    """Where a reused document was before an attach, to put it back on rollback."""

    rendicion_id: int | None
    role: DocRole


class RendicionService:
    """Facade used by the API and the CLI."""

    def __init__(self, settings: Settings, classifier: Classifier) -> None:
        prepare_data_dir(settings)
        self.settings = settings
        self.repo = Repository(settings.db_path)
        self.store = FileStore(settings.files_dir, settings.tmp_dir, settings.max_upload_bytes)
        self.classifier = classifier

    # ------------------------------------------------------------------ ingest

    def ingest(
        self,
        stream: BinaryIO,
        filename: str,
        relative_path: str | None,
        batch_id: str | None,
    ) -> FileResult:
        """Store, recognise and persist one uploaded file (not yet grouped)."""
        name = sanitize_filename(filename or "archivo")
        try:
            folder = sanitize_relative_folder(relative_path)
            stored = self.store.save_stream(stream)
        except UploadError as exc:
            return FileResult(name=name, ok=False, error=str(exc))
        existing = self.repo.find_document_by_sha(stored.sha256)
        if existing is not None:
            logger.info("duplicate upload of document %d", existing.id)
            return FileResult(
                name=name,
                ok=True,
                document_id=existing.id,
                duplicate=True,
                doc_type=existing.extraction.doc_type.value,
                rendicion_id=existing.rendicion_id,
            )
        text, method = extract_text(stored.path, stored.mime)
        extraction = self.classifier.classify(
            RecognitionInput(path=stored.path, mime=stored.mime, text=text, folder=folder)
        )
        doc_id = self.repo.add_document(
            sha256=stored.sha256,
            original_name=name,
            folder=folder,
            path=stored.path,
            mime=stored.mime,
            size=stored.size,
            extraction=extraction,
            batch_id=batch_id,
        )
        logger.info(
            "document %d ingested (method=%s, classifier=%s)", doc_id, method, extraction.classifier
        )
        return FileResult(
            name=name, ok=True, document_id=doc_id, doc_type=extraction.doc_type.value
        )

    # ---------------------------------------------------------------- grouping

    def group_pending(self, batch_id: str | None = None) -> GroupingResult:
        """Group ungrouped documents, possibly joining existing borrador rendiciones."""
        pending = self.repo.ungrouped_documents(batch_id)
        result = GroupingResult()
        if not pending:
            return result
        drafts = [r.id for r in self.repo.list_rendiciones(Status.BORRADOR)]
        existing_docs = [d for d in self.repo.documents_of(drafts) if d.role == DocRole.SOPORTE]
        items = [to_item(d) for d in pending + existing_docs]
        pending_ids = {d.id for d in pending}
        for group in group_items(items):
            new_ids = [i for i in group.doc_ids if i in pending_ids]
            if not new_ids:
                continue
            if group.rendicion_id is not None:
                rid = group.rendicion_id
                result.extended.append(rid)
                self.repo.add_event(rid, "documentos_agregados", {"document_ids": new_ids})
            else:
                rid = self.repo.create_rendicion(confidence=group.confidence, reason=group.reason)
                result.created.append(rid)
            for doc_id in new_ids:
                self.repo.set_document_rendicion(doc_id, rid)
            self.repo.set_grouping(rid, group.confidence, group.reason)
            self.refresh_summary(rid)
        logger.info(
            "grouping: %d docs, %d created, %d extended",
            len(pending),
            len(result.created),
            len(result.extended),
        )
        return result

    def refresh_summary(self, rendicion_id: int) -> None:
        """Recompute fecha/monto/tipo from documents unless the user edited them."""
        rendicion = self.repo.get_rendicion(rendicion_id)
        if rendicion.fields_locked:
            return
        summary = summarize([to_item(d) for d in rendicion.support_documents])
        self.repo.update_fields(
            rendicion_id,
            {
                "tipo_prestacion": summary.tipo_prestacion,
                "fecha_atencion": summary.fecha_atencion,
                "monto": summary.monto,
            },
            lock=False,
        )

    # ----------------------------------------------------------------- editing

    def _require_editable(self, rendicion: RendicionView) -> None:
        if rendicion.status not in EDITABLE_STATUSES:
            raise NotEditableError(
                f"La rendición {rendicion.id} está en estado {rendicion.status} "
                "y no se puede editar"
            )

    def edit(
        self,
        rendicion_id: int,
        *,
        tipo_prestacion: TipoPrestacion | None = None,
        fecha_atencion: date | None = None,
        monto: int | None = None,
    ) -> RendicionView:
        """Apply user edits; edited fields are no longer recomputed automatically."""
        rendicion = self.repo.get_rendicion(rendicion_id)
        self._require_editable(rendicion)
        values: dict[str, object] = {}
        if tipo_prestacion is not None:
            values["tipo_prestacion"] = tipo_prestacion
        if fecha_atencion is not None:
            values["fecha_atencion"] = fecha_atencion
        if monto is not None:
            if monto < 0:
                raise ValueError("El monto no puede ser negativo")
            values["monto"] = monto
        self.repo.update_fields(rendicion_id, values, lock=True)
        return self.repo.get_rendicion(rendicion_id)

    def recompute(self, rendicion_id: int) -> RendicionView:
        """Drop the user's manual values and recompute fecha/monto/tipo from documents."""
        self._require_editable(self.repo.get_rendicion(rendicion_id))
        self.repo.unlock_fields(rendicion_id)
        self.refresh_summary(rendicion_id)
        self._demote_if_incomplete(rendicion_id, via="recalculado")
        return self.repo.get_rendicion(rendicion_id)

    def restore_grouping(self, rendicion_id: int, confidence: float, reason: str) -> RendicionView:
        """Put back the grouping confidence and reason a rendición had before a move was undone."""
        self._require_editable(self.repo.get_rendicion(rendicion_id))
        if not 0 <= confidence <= 1:
            raise ValueError("La confianza debe estar entre 0 y 1")
        self.repo.set_grouping(rendicion_id, confidence, reason)
        self.repo.add_event(
            rendicion_id, "agrupacion_restaurada", {"confidence": confidence, "reason": reason}
        )
        return self.repo.get_rendicion(rendicion_id)

    def edit_document(self, doc_id: int, edit: DocumentEdit) -> DocumentView:
        """Correct a document's extracted fields and refresh its rendición's summary."""
        doc = self.repo.get_document(doc_id)
        if doc.rendicion_id is not None:
            self._require_editable(self.repo.get_rendicion(doc.rendicion_id))
        if not edit.sent:
            return doc
        self.repo.update_document_fields(doc_id, edit)
        if doc.rendicion_id is not None:
            self.repo.add_event(
                doc.rendicion_id,
                "documento_editado",
                {"document_id": doc_id, "fields": sorted(edit.sent)},
            )
            self.refresh_summary(doc.rendicion_id)
            self._demote_if_incomplete(doc.rendicion_id, via="documento_editado")
        return self.repo.get_document(doc_id)

    def _demote_if_incomplete(self, rendicion_id: int, *, via: str) -> None:
        """Send a lista rendición back to borrador when it lost tipo, fecha or monto."""
        rendicion = self.repo.get_rendicion(rendicion_id)
        if rendicion.status != Status.LISTA:
            return
        summary = {
            "tipo_prestacion": rendicion.tipo_prestacion,
            "fecha_atencion": rendicion.fecha_atencion,
            "monto": rendicion.monto,
        }
        missing = sorted(name for name, value in summary.items() if value is None)
        if missing:
            self.repo.transition(
                rendicion_id,
                Status.BORRADOR,
                kind="estado",
                detail={"via": via, "missing": missing},
            )

    def move_document(self, doc_id: int, target_rendicion_id: int | None) -> RendicionView:
        """Move a document to another rendición, or to a new one when target is None."""
        result = self.move_documents([doc_id], target_rendicion_id)
        return self.repo.get_rendicion(result.rendicion_id)

    def move_documents(self, doc_ids: list[int], target_rendicion_id: int | None) -> MoveResult:
        """Move documents to one rendición (a new one when target is None).

        Every source and the target are checked before anything changes; sources left
        without documents are deleted.
        """
        docs = [self.repo.get_document(doc_id) for doc_id in dict.fromkeys(doc_ids)]
        sources = list(dict.fromkeys(d.rendicion_id for d in docs if d.rendicion_id is not None))
        for source_id in sources:
            self._require_editable(self.repo.get_rendicion(source_id))
        if target_rendicion_id is None:
            target_rendicion_id = self.repo.create_rendicion(
                confidence=1.0, reason="creada manualmente"
            )
        else:
            self._require_editable(self.repo.get_rendicion(target_rendicion_id))
        moved = [d for d in docs if d.rendicion_id != target_rendicion_id]
        result = MoveResult(rendicion_id=target_rendicion_id)
        if not moved:
            return result
        for doc in moved:
            self.repo.set_document_rendicion(doc.id, target_rendicion_id)
        self.repo.add_event(
            target_rendicion_id, "documento_movido", {"document_ids": [d.id for d in moved]}
        )
        self.repo.set_grouping(target_rendicion_id, 1.0, "ajustada manualmente")
        self.refresh_summary(target_rendicion_id)
        for source_id in sources:
            if source_id == target_rendicion_id:
                continue
            removed = [d.id for d in moved if d.rendicion_id == source_id]
            if self._after_removal(source_id, removed):
                result.deleted_rendiciones.append(source_id)
        return result

    def _after_removal(self, rendicion_id: int, doc_ids: list[int]) -> bool:
        """Delete the rendición if it was left empty (return True), else refresh it."""
        source = self.repo.get_rendicion(rendicion_id)
        if not source.documents:
            self.repo.delete_rendicion(rendicion_id)
            logger.info("rendicion %d removed (empty)", rendicion_id)
            return True
        self.repo.add_event(rendicion_id, "documento_quitado", {"document_ids": doc_ids})
        self.repo.set_grouping(rendicion_id, 1.0, "ajustada manualmente")
        self.refresh_summary(rendicion_id)
        self._demote_if_incomplete(rendicion_id, via="documento_quitado")
        return False

    def delete_document(self, doc_id: int) -> None:
        """Delete a document (row and file)."""
        doc = self.repo.get_document(doc_id)
        if doc.rendicion_id is not None:
            self._require_editable(self.repo.get_rendicion(doc.rendicion_id))
        self.repo.delete_document(doc_id)
        self.store.delete(doc.path)
        if doc.rendicion_id is not None:
            self._after_removal(doc.rendicion_id, [doc_id])

    def delete_rendicion(self, rendicion_id: int) -> int:
        """Delete a rendición with its documents and files; return files removed."""
        self._require_editable(self.repo.get_rendicion(rendicion_id))
        docs = self.repo.delete_rendicion(rendicion_id)
        for doc in docs:
            self.store.delete(doc.path)
        logger.info("rendicion %d deleted with %d documents", rendicion_id, len(docs))
        return len(docs)

    def set_status(
        self, rendicion_id: int, target: Status, error_message: str | None = None
    ) -> RendicionView:
        """Generic, state-machine-validated status change."""
        updates: dict[str, object] = {}
        if target == Status.ERROR:
            updates["error_message"] = (error_message or "Error sin detalle")[:500]
        return self.repo.transition(
            rendicion_id, target, kind="estado", detail={"via": "api"}, updates=updates
        )

    def attach_file(
        self, rendicion_id: int, source: Path | BinaryIO, filename: str, role: DocRole
    ) -> DocumentView:
        """Store a file and attach it to a rendición with the given role."""
        return self._attach(rendicion_id, source, filename, role)[0]

    def attach_files(
        self, rendicion_id: int, paths: Sequence[Path], role: DocRole
    ) -> list[DocumentView]:
        """Attach every file or none: a failed batch leaves the rendición as it found it."""
        main_before = self.repo.get_rendicion(rendicion_id).bonificacion_document_id
        attached: list[tuple[DocumentView, _Placement | None]] = []
        try:
            for path in paths:
                attached.append(self._attach(rendicion_id, path, path.name, role))
        except Exception:
            for doc, before in reversed(attached):
                if before is None:
                    self.repo.delete_document(doc.id)
                    self.store.delete(doc.path)
                else:
                    self.repo.set_document_rendicion(doc.id, before.rendicion_id)
                    self.repo.set_document_role(doc.id, before.role)
            if attached and role == DocRole.BONIFICACION_COLMENA:
                self.repo.record_fields(
                    rendicion_id,
                    {"bonificacion_document_id": main_before},
                    kind="adjuntos_revertidos",
                    detail={"document_ids": [doc.id for doc, _ in attached]},
                )
            logger.warning("rendicion %d: attach batch rolled back", rendicion_id)
            raise
        return [doc for doc, _ in attached]

    def _attach(
        self, rendicion_id: int, source: Path | BinaryIO, filename: str, role: DocRole
    ) -> tuple[DocumentView, _Placement | None]:
        """Attach one file; also return where a reused document was before (None if new)."""
        rendicion = self.repo.get_rendicion(rendicion_id)
        if isinstance(source, Path):
            stored = self.store.save_path(source)
        else:
            stored = self.store.save_stream(source)
        existing = self.repo.find_document_by_sha(stored.sha256)
        before = None
        if existing is not None:
            before = _Placement(existing.rendicion_id, existing.role)
            if existing.rendicion_id not in (None, rendicion_id):
                raise UploadError("El archivo ya pertenece a otra rendición")
            doc_id = existing.id
            self.repo.set_document_rendicion(doc_id, rendicion_id)
            self.repo.set_document_role(doc_id, role)
        else:
            text, _ = extract_text(stored.path, stored.mime)
            extraction = self.classifier.classify(
                RecognitionInput(path=stored.path, mime=stored.mime, text=text)
            )
            doc_id = self.repo.add_document(
                sha256=stored.sha256,
                original_name=sanitize_filename(filename),
                folder="",
                path=stored.path,
                mime=stored.mime,
                size=stored.size,
                extraction=extraction,
                rendicion_id=rendicion.id,
                role=role,
            )
        if role == DocRole.BONIFICACION_COLMENA:
            self.repo.record_fields(
                rendicion_id,
                {"bonificacion_document_id": doc_id},
                kind="comprobante_adjuntado",
                detail={"document_id": doc_id},
            )
        elif role == DocRole.RESPUESTA_CHUBB:
            self.repo.add_event(rendicion_id, "liquidacion_adjuntada", {"document_id": doc_id})
        else:
            self.repo.add_event(
                rendicion_id, "documento_adjuntado", {"document_id": doc_id, "role": role.value}
            )
        return self.repo.get_document(doc_id), before
