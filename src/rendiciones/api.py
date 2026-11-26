"""FastAPI app: upload UI, rendiciones REST API and automation report-back endpoints."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import asdict
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from rendiciones.accounts import (
    STATE_FILE,
    AccountService,
    LoginBusyError,
    LoginLog,
    browser_login_runner,
)
from rendiciones.bank.settings import BankSettingsStore
from rendiciones.bank.settings import validate as validate_bank_settings
from rendiciones.config import CSRF_HEADER, CSRF_VALUE, MAX_FILES_PER_REQUEST, Settings
from rendiciones.credentials import MAX_FIELD_LENGTH, CredentialStoreError, KeychainStore
from rendiciones.db import NotFoundError, extraction_to_dict
from rendiciones.domain import (
    DocRole,
    DocType,
    DocumentEdit,
    DocumentView,
    InvalidTransitionError,
    RendicionView,
    Status,
    TipoPrestacion,
)
from rendiciones.locking import AutomationBusyError, AutomationLock
from rendiciones.pipeline import Outcome, Portal, record_result, record_submission
from rendiciones.recognition.base import Classifier
from rendiciones.runner import (
    BankFactory,
    ClientsFactory,
    PipelineRunner,
    RunMode,
    bank_factory,
    clients_factory,
)
from rendiciones.rut import normalize as normalize_rut
from rendiciones.services import NotEditableError, RendicionService
from rendiciones.storage import UploadError
from rendiciones.workflow import (
    ACCOUNTS,
    Workflow,
    WorkflowError,
    WorkflowStore,
    account_json,
    build,
    check,
    progress,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}
DOCUMENT_FILE_PATH = re.compile(r"/api/documents/\d+/file")
BATCH_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# --------------------------------------------------------------------- schemas


class RendicionPatch(BaseModel):
    """Editable fields of a rendición."""

    tipo_prestacion: TipoPrestacion | None = None
    fecha_atencion: date | None = None
    monto: int | None = Field(default=None, ge=0)


class StatusChange(BaseModel):
    """Requested status change."""

    status: Status
    error_message: str | None = Field(default=None, max_length=500)


class MoveRequest(BaseModel):
    """Target rendición for a document (null = new rendición)."""

    rendicion_id: int | None = None


class RestoreGroupingPayload(BaseModel):
    """Grouping confidence and reason to put back on a rendición."""

    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=200)


class BulkMovePayload(BaseModel):
    """Documents to move together to one rendición (null = one new rendición)."""

    document_ids: list[int] = Field(min_length=1)
    rendicion_id: int | None = None


class DocumentPatchPayload(BaseModel):
    """User corrections to a document's extracted fields; explicit null clears one."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    doc_type: DocType | None = None
    fecha: date | None = None
    monto: int | None = Field(default=None, ge=0)
    provider_name: str | None = Field(default=None, min_length=1, max_length=200)
    provider_rut: str | None = None
    folio: str | None = Field(default=None, min_length=1, max_length=50)
    tipo_prestacion: TipoPrestacion | None = None
    patient_rut: str | None = None

    @field_validator("doc_type")
    @classmethod
    def _require_doc_type(cls, value: DocType | None) -> DocType:
        if value is None:
            raise ValueError("El tipo de documento no se puede borrar")
        return value

    @field_validator("provider_rut", "patient_rut")
    @classmethod
    def _canonicalize_rut(cls, value: str | None) -> str | None:
        if value is None:
            return None
        canonical = normalize_rut(value)
        if canonical is None:
            raise ValueError("RUT inválido")
        return canonical


class SubmissionIn(BaseModel):
    """Report that a rendición was submitted to a portal."""

    portal: Portal
    request_id: str = Field(min_length=1, max_length=120)
    raw: dict[str, Any] | None = None


class ResultIn(BaseModel):
    """Report a portal decision."""

    portal: Portal
    outcome: Outcome
    monto: int | None = Field(default=None, ge=0)
    detail: dict[str, Any] | None = None


class RunIn(BaseModel):
    """Pipeline pass to start from the UI."""

    mode: RunMode


class WorkflowIn(BaseModel):
    """Stages of account keys; accounts in one stage run side by side."""

    stages: list[list[str]] = Field(max_length=20)


class AccountKey(StrEnum):
    """Credentials kept in the keychain: both portals and the bank."""

    COLMENA = "colmena"
    CHUBB = "chubb"
    BANCOCHILE = "bancochile"


class BankSettingsIn(BaseModel):
    """Glosas that identify each insurer's transfer, and the days to wait for it."""

    glosas: dict[str, list[str]]
    wait_days: int


class AccountIn(BaseModel):
    """Credential for a portal. The password is never echoed back or logged."""

    username: str = Field(min_length=1, max_length=MAX_FIELD_LENGTH)
    password: SecretStr = Field(min_length=1, max_length=MAX_FIELD_LENGTH)


# ------------------------------------------------------------------ serializers


def document_json(doc: DocumentView) -> dict[str, Any]:
    """Serialize a document, including its absolute path for automation."""
    return {
        "id": doc.id,
        "rendicion_id": doc.rendicion_id,
        "original_name": doc.original_name,
        "folder": doc.folder,
        "path": str(doc.path.resolve()),
        "mime": doc.mime,
        "size": doc.size,
        "sha256": doc.sha256,
        "role": doc.role.value,
        "created_at": doc.created_at.isoformat(),
        **extraction_to_dict(doc.extraction),
        "edited_fields": sorted(doc.edited_fields),
    }


def rendicion_json(r: RendicionView, workflow: Workflow) -> dict[str, Any]:
    """Serialize a rendición with its documents and per-account progress."""

    def iso(value: Any) -> Any:
        return value.isoformat() if value is not None else None

    return {
        "id": r.id,
        "status": r.status.value,
        "tipo_prestacion": r.tipo_prestacion.value if r.tipo_prestacion else None,
        "fecha_atencion": iso(r.fecha_atencion),
        "monto": r.monto,
        "grouping_confidence": r.grouping_confidence,
        "grouping_reason": r.grouping_reason,
        "fields_locked": r.fields_locked,
        "error_message": r.error_message,
        "status_before_error": r.status_before_error.value if r.status_before_error else None,
        "colmena_request_id": r.colmena_request_id,
        "colmena_result": r.colmena_result,
        "colmena_monto_bonificado": r.colmena_monto_bonificado,
        "bonificacion_document_id": r.bonificacion_document.id if r.bonificacion_document else None,
        "chubb_request_id": r.chubb_request_id,
        "chubb_result": r.chubb_result,
        "chubb_monto_reembolsado": r.chubb_monto_reembolsado,
        "created_at": iso(r.created_at),
        "updated_at": iso(r.updated_at),
        "colmena_submitted_at": iso(r.colmena_submitted_at),
        "colmena_resolved_at": iso(r.colmena_resolved_at),
        "chubb_submitted_at": iso(r.chubb_submitted_at),
        "chubb_resolved_at": iso(r.chubb_resolved_at),
        "documents": [document_json(d) for d in r.documents],
        "progress": progress(r, workflow),
    }


# ------------------------------------------------------------------ middleware


class SecurityMiddleware(BaseHTTPMiddleware):
    """Host allow-list (anti DNS-rebinding) and CSRF checks on mutating requests."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Reject foreign hosts and cross-site writes."""
        host = request.headers.get("host", "").rsplit(":", 1)[0].lower()
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "Host no permitido"}, status_code=400)
        if request.method not in SAFE_METHODS:
            if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
                return JSONResponse(
                    {"detail": f"Falta el encabezado {CSRF_HEADER}"}, status_code=403
                )
            origin = request.headers.get("origin")
            if origin:
                origin_host = re.sub(r"^https?://", "", origin).rsplit(":", 1)[0].lower()
                if origin_host not in ALLOWED_HOSTS:
                    return JSONResponse({"detail": "Origen no permitido"}, status_code=403)
        response = await call_next(request)
        # Stored documents are previewed in an iframe of the UI; nothing else is framable.
        framable = DOCUMENT_FILE_PATH.fullmatch(request.url.path) is not None
        frame_ancestors = "'self'" if framable else "'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN" if framable else "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            # Revalidate (ETag) on every load so an updated UI never runs with stale JS/CSS.
            response.headers["Cache-Control"] = "no-cache"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            f"frame-ancestors {frame_ancestors}"
        )
        return response


# ---------------------------------------------------------------------- app


def create_app(
    settings: Settings,
    classifier: Classifier,
    accounts: AccountService | None = None,
    portal_clients: ClientsFactory | None = None,
    bank: BankFactory | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    ``portal_clients`` builds the clients for passes started from the UI (Playwright by
    default, or ``RENDICIONES_PORTAL_FACTORY``).
    """
    service = RendicionService(settings, classifier)
    lock = AutomationLock.for_data_dir(settings.data_dir)
    accounts = accounts or AccountService(
        KeychainStore(), LoginLog(settings.data_dir / STATE_FILE), browser_login_runner, lock
    )
    workflows = WorkflowStore.for_data_dir(settings.data_dir)
    bank_settings = BankSettingsStore.for_data_dir(settings.data_dir)
    if bank is None and portal_clients is None:
        bank = bank_factory(None, settings)
    runner = PipelineRunner(
        service, lock, portal_clients or clients_factory(), workflows, bank=bank
    )

    def view(r: RendicionView) -> dict[str, Any]:
        return rendicion_json(r, workflows.read())

    def workflow_json(workflow: Workflow) -> dict[str, Any]:
        return {
            "stages": workflow.as_json(),
            "accounts": [account_json(spec) for spec in ACCOUNTS.values()],
        }

    app = FastAPI(title="Rendiciones de salud", docs_url="/api/docs", redoc_url=None)
    app.state.service = service
    app.add_middleware(SecurityMiddleware)

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse({"detail": f"No encontrado: {exc}"}, status_code=404)

    @app.exception_handler(InvalidTransitionError)
    async def _bad_transition(_: Request, exc: InvalidTransitionError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(NotEditableError)
    async def _not_editable(_: Request, exc: NotEditableError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(UploadError)
    async def _upload_error(_: Request, exc: UploadError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(CredentialStoreError)
    async def _keychain_error(_: Request, exc: CredentialStoreError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "classifier": classifier.name}

    @app.post("/api/uploads")
    def upload(
        files: Annotated[list[UploadFile], File(description="Archivos a subir")],
        paths: Annotated[list[str] | None, Form()] = None,
        batch_id: Annotated[str | None, Form()] = None,
        group: Annotated[bool, Form()] = True,
    ) -> dict[str, Any]:
        """Upload files. ``paths[i]`` is the browser relative path of ``files[i]``.

        With ``group=false`` documents stay ungrouped until ``POST /api/batches/{id}/group``.
        """
        if len(files) > MAX_FILES_PER_REQUEST:
            raise HTTPException(400, f"Máximo {MAX_FILES_PER_REQUEST} archivos por envío")
        if batch_id is not None and not BATCH_RE.fullmatch(batch_id):
            raise HTTPException(400, "batch_id inválido")
        batch = batch_id or uuid.uuid4().hex
        rel_paths = paths or []
        results = []
        for index, upload_file in enumerate(files):
            rel = rel_paths[index] if index < len(rel_paths) else None
            results.append(service.ingest(upload_file.file, upload_file.filename or "", rel, batch))
        grouping = service.group_pending(batch) if group else None
        if grouping is not None:
            for res in results:
                if res.document_id is not None and res.rendicion_id is None:
                    res.rendicion_id = service.repo.get_document(res.document_id).rendicion_id
        logger.info(
            "upload batch: %d files, %d errors, %d duplicates",
            len(results),
            sum(not r.ok for r in results),
            sum(r.duplicate for r in results),
        )
        return {
            "batch_id": batch,
            "files": [r.__dict__ for r in results],
            "duplicates": [r.__dict__ for r in results if r.duplicate],
            "grouping": grouping.__dict__ if grouping else None,
        }

    @app.post("/api/batches/{batch_id}/group")
    def group_batch(batch_id: str) -> dict[str, Any]:
        """Group the ungrouped documents of a batch."""
        if not BATCH_RE.fullmatch(batch_id):
            raise HTTPException(400, "batch_id inválido")
        return service.group_pending(batch_id).__dict__

    @app.get("/api/rendiciones")
    def list_rendiciones(status: Status | None = None) -> list[dict[str, Any]]:
        """List rendiciones, optionally filtered by status."""
        workflow = workflows.read()
        return [rendicion_json(r, workflow) for r in service.repo.list_rendiciones(status)]

    @app.get("/api/rendiciones/{rendicion_id}")
    def get_rendicion(rendicion_id: int) -> dict[str, Any]:
        """One rendición with absolute document paths."""
        return view(service.repo.get_rendicion(rendicion_id))

    @app.patch("/api/rendiciones/{rendicion_id}")
    def patch_rendicion(rendicion_id: int, body: RendicionPatch) -> dict[str, Any]:
        """Edit tipo de prestación, fecha de atención or monto."""
        return view(
            service.edit(
                rendicion_id,
                tipo_prestacion=body.tipo_prestacion,
                fecha_atencion=body.fecha_atencion,
                monto=body.monto,
            )
        )

    @app.post("/api/rendiciones/{rendicion_id}/recompute")
    def recompute_rendicion(rendicion_id: int) -> dict[str, Any]:
        """Discard manual tipo/fecha/monto and recompute them from the documents."""
        return view(service.recompute(rendicion_id))

    @app.put("/api/rendiciones/{rendicion_id}/grouping")
    def restore_grouping(rendicion_id: int, payload: RestoreGroupingPayload) -> dict[str, Any]:
        """Put back a rendición's grouping confidence and reason (undoing a move)."""
        return view(service.restore_grouping(rendicion_id, payload.confidence, payload.reason))

    @app.delete("/api/rendiciones/{rendicion_id}")
    def delete_rendicion(rendicion_id: int) -> dict[str, Any]:
        """Delete a rendición (borrador/lista only) and its files."""
        return {"deleted_documents": service.delete_rendicion(rendicion_id)}

    @app.post("/api/rendiciones/{rendicion_id}/status")
    def change_status(rendicion_id: int, body: StatusChange) -> dict[str, Any]:
        """State-machine-validated status change (e.g. borrador → lista, error)."""
        return view(service.set_status(rendicion_id, body.status, body.error_message))

    @app.post("/api/rendiciones/{rendicion_id}/submissions")
    def submission(rendicion_id: int, body: SubmissionIn) -> dict[str, Any]:
        """Report that a rendición was submitted to Colmena or Chubb."""
        return view(
            record_submission(service, rendicion_id, body.portal, body.request_id, body.raw)
        )

    @app.post("/api/rendiciones/{rendicion_id}/results")
    def result(rendicion_id: int, body: ResultIn) -> dict[str, Any]:
        """Report an approval or rejection from a portal."""
        return view(
            record_result(service, rendicion_id, body.portal, body.outcome, body.monto, body.detail)
        )

    @app.post("/api/rendiciones/{rendicion_id}/documents")
    def attach(
        rendicion_id: int,
        file: Annotated[UploadFile, File()],
        role: Annotated[DocRole, Form()] = DocRole.SOPORTE,
    ) -> dict[str, Any]:
        """Attach a file (e.g. the Colmena comprobante de bonificación)."""
        doc = service.attach_file(rendicion_id, file.file, file.filename or "archivo", role)
        return document_json(doc)

    @app.get("/api/rendiciones/{rendicion_id}/events")
    def events(rendicion_id: int) -> list[dict[str, Any]]:
        """History of a rendición."""
        service.repo.get_rendicion(rendicion_id)
        return [
            {
                "id": e.id,
                "at": e.at.isoformat(),
                "kind": e.kind,
                "from_status": e.from_status.value if e.from_status else None,
                "to_status": e.to_status.value if e.to_status else None,
                "detail": e.detail,
            }
            for e in service.repo.events(rendicion_id)
        ]

    @app.post("/api/documents/move")
    def move_many(payload: BulkMovePayload) -> dict[str, Any]:
        """Move documents to one rendición (``null`` = a single new one); empty sources go."""
        return asdict(service.move_documents(payload.document_ids, payload.rendicion_id))

    @app.patch("/api/documents/{doc_id}")
    def patch_document(doc_id: int, payload: DocumentPatchPayload) -> dict[str, Any]:
        """Correct a document's fields; its rendición's summary is recomputed unless locked."""
        edit = DocumentEdit.build(**payload.model_dump(exclude_unset=True))
        return document_json(service.edit_document(doc_id, edit))

    @app.post("/api/documents/{doc_id}/move")
    def move(doc_id: int, body: MoveRequest) -> dict[str, Any]:
        """Move a document to another rendición or to a new one."""
        return view(service.move_document(doc_id, body.rendicion_id))

    @app.delete("/api/documents/{doc_id}")
    def delete_document(doc_id: int) -> dict[str, Any]:
        """Delete a document and its file."""
        service.delete_document(doc_id)
        return {"deleted": doc_id}

    @app.get("/api/documents/{doc_id}/file")
    def document_file(doc_id: int) -> FileResponse:
        """Serve the stored file (inline)."""
        doc = service.repo.get_document(doc_id)
        if settings.files_dir.resolve() not in doc.path.resolve().parents:
            raise HTTPException(404, "Archivo no disponible")
        return FileResponse(doc.path, media_type=doc.mime, content_disposition_type="inline")

    @app.get("/api/accounts")
    def list_accounts() -> list[dict[str, Any]]:
        """Portal accounts: whether a credential is saved and the last login result."""
        return accounts.list()

    @app.put("/api/accounts/{portal}")
    def save_account(portal: AccountKey, body: AccountIn) -> dict[str, Any]:
        """Save (or replace) a portal credential in the keychain."""
        try:
            return accounts.save(portal.value, body.username, body.password.get_secret_value())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/accounts/{portal}")
    def delete_account(portal: AccountKey) -> dict[str, Any]:
        """Forget a portal credential."""
        return {"deleted": accounts.delete(portal.value)}

    @app.post("/api/accounts/{portal}/login")
    def login_account(portal: AccountKey) -> dict[str, Any]:
        """Open the portal in a visible browser and log in (autofilled when saved)."""
        try:
            return accounts.start_login(portal.value)
        except LoginBusyError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/bank/settings")
    def get_bank_settings() -> dict[str, Any]:
        """Glosas searched in the bank per insurer and the days to wait for a transfer."""
        return bank_settings.read().as_json()

    @app.put("/api/bank/settings")
    def save_bank_settings(body: BankSettingsIn) -> dict[str, Any]:
        """Save what the abono nodes look for in the cartola."""
        try:
            return bank_settings.save(validate_bank_settings(body.model_dump())).as_json()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/pipeline")
    def pipeline_status() -> dict[str, Any]:
        """Last pass started from the UI and whether the browser is in use."""
        return runner.status()

    @app.post("/api/pipeline/runs", status_code=202)
    def start_pipeline(body: RunIn) -> dict[str, Any]:
        """Start one pass in the background (``submit`` sends to the portals for real)."""
        try:
            return runner.start(body.mode)
        except AutomationBusyError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/workflow")
    def get_workflow() -> dict[str, Any]:
        """Saved workflow plus the accounts that can take part in it."""
        return workflow_json(workflows.read())

    @app.post("/api/workflow/check")
    def check_workflow(body: WorkflowIn) -> dict[str, Any]:
        """Problems that would keep ``stages`` from running (nothing is saved)."""
        return {"problems": check(body.stages)}

    @app.put("/api/workflow")
    def save_workflow(body: WorkflowIn) -> dict[str, Any]:
        """Save the workflow used by the next pipeline pass."""
        try:
            return workflow_json(workflows.save(build(body.stages)))
        except WorkflowError as exc:
            raise HTTPException(422, exc.problems) from exc

    @app.delete("/api/workflow")
    def reset_workflow() -> dict[str, Any]:
        """Go back to the default workflow (Colmena → Chubb)."""
        return workflow_json(workflows.reset())

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        """Enumerations for the UI."""
        return {
            "statuses": [s.value for s in Status],
            "tipos_prestacion": [t.value for t in TipoPrestacion],
            "csrf_header": CSRF_HEADER,
        }

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
