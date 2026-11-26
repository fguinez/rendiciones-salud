"""Submission pipeline: lista → Colmena → (comprobante) → Chubb (liquidación), plus bank checks.

Also exposes ``record_submission`` / ``record_result`` so that automation driven
externally (e.g. an AI agent using a browser) can report back through the REST API
with exactly the same state transitions.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from rendiciones.automation.base import (
    ColmenaClient,
    LiquidacionProvider,
    PortalClient,
    PortalError,
    PortalState,
    SessionExpiredError,
    StatusResult,
    SubmissionResult,
)
from rendiciones.bank import BankClient, BankError, BankMovement
from rendiciones.bank.expected import expected_transfer
from rendiciones.bank.matching import Expected, glosa_matches, match_transfers
from rendiciones.bank.settings import BankSettings
from rendiciones.db import utcnow
from rendiciones.domain import DocRole, InvalidTransitionError, RendicionView, Status
from rendiciones.services import RendicionService
from rendiciones.workflow import (
    ABONADO,
    ACCOUNTS,
    DEFAULT_WORKFLOW,
    NO_LLEGO,
    Workflow,
    approved_monto,
)

logger = logging.getLogger(__name__)

# How long after Chubb's decision the pipeline keeps looking for its liquidación PDF.
LIQUIDACION_WINDOW = timedelta(days=30)

Step = Callable[["PipelineReport"], None]


class Portal(StrEnum):
    """Target insurer."""

    COLMENA = "colmena"
    CHUBB = "chubb"


class Outcome(StrEnum):
    """Final decision reported for a request."""

    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class PipelineReport:
    """Counts of what a pipeline run did (ids only, no PII)."""

    colmena_submitted: list[int] = field(default_factory=list)
    colmena_approved: list[int] = field(default_factory=list)
    colmena_rejected: list[int] = field(default_factory=list)
    chubb_submitted: list[int] = field(default_factory=list)
    chubb_approved: list[int] = field(default_factory=list)
    chubb_rejected: list[int] = field(default_factory=list)
    pending: list[int] = field(default_factory=list)
    errors: list[int] = field(default_factory=list)
    dry_run: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    sessions_expired: list[str] = field(default_factory=list)
    chubb_liquidaciones: list[int] = field(default_factory=list)
    abonos_confirmed: list[int] = field(default_factory=list)
    abonos_waiting: list[int] = field(default_factory=list)
    abonos_missing: list[int] = field(default_factory=list)
    bank_errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[Any]]:
        """Plain dict for JSON / CLI output."""
        return {k: list(v) for k, v in self.__dict__.items()}


def record_submission(
    service: RendicionService,
    rendicion_id: int,
    portal: Portal,
    request_id: str,
    raw: dict[str, Any] | None = None,
) -> RendicionView:
    """Mark a rendición as submitted to ``portal`` with the portal's request id."""
    now = utcnow()
    if portal == Portal.COLMENA:
        target, updates = (
            Status.COLMENA_ENVIADA,
            {
                "colmena_request_id": request_id,
                "colmena_submitted_at": now,
            },
        )
    else:
        target, updates = (
            Status.CHUBB_ENVIADA,
            {
                "chubb_request_id": request_id,
                "chubb_submitted_at": now,
            },
        )
        current = service.repo.get_rendicion(rendicion_id)
        if current.bonificacion_document is None:
            raise InvalidTransitionError(current.status, target)
    view = service.repo.transition(
        rendicion_id,
        target,
        kind=f"{portal.value}_enviada",
        detail={"request_id": request_id, **({"raw": raw} if raw else {})},
        updates=updates,
    )
    service.repo.clear_marker(rendicion_id, portal.value)
    return view


def record_result(
    service: RendicionService,
    rendicion_id: int,
    portal: Portal,
    outcome: Outcome,
    monto: int | None = None,
    detail: dict[str, Any] | None = None,
) -> RendicionView:
    """Record an approval/rejection reported by a portal (or an external agent)."""
    approved = outcome == Outcome.APPROVED
    result = {"outcome": outcome.value, "monto": monto, **(detail or {})}
    now = utcnow()
    if portal == Portal.COLMENA:
        target = Status.COLMENA_APROBADA if approved else Status.COLMENA_RECHAZADA
        updates: dict[str, Any] = {
            "colmena_result_json": result,
            "colmena_monto_bonificado": monto,
            "colmena_resolved_at": now,
        }
    else:
        target = Status.CHUBB_APROBADA if approved else Status.CHUBB_RECHAZADA
        updates = {
            "chubb_result_json": result,
            "chubb_monto_reembolsado": monto,
            "chubb_resolved_at": now,
        }
    return service.repo.transition(
        rendicion_id, target, kind=f"{portal.value}_resultado", detail=result, updates=updates
    )


def error_text(prefix: str, exc: Exception) -> str:
    """Message stored on a failed rendición: safe portal messages verbatim, else the type."""
    if isinstance(exc, PortalError):
        return str(exc)
    return f"{prefix}: {type(exc).__name__}"


class Pipeline:
    """Drives rendiciones through both portals using PortalClient implementations.

    With ``dry_run`` the clients are called to fill the forms (they must stop before any
    irreversible action), no status changes are recorded and polling is skipped.

    A ``SessionExpiredError`` moves only the rendición being processed to ``error``; the
    portal is then skipped for the rest of the run so an expired session cannot fail
    every pending rendición at once.

    ``workflow`` sets which portals receive new submissions and in what order. Requests
    already sent to a portal outside the workflow are still polled to their outcome.

    The ``abono_*`` nodes, only when in the workflow, look for each insurer's approved
    reimbursements in the bank (``bank``). The cartola is read at most once per run and
    only when some reimbursement is still waiting; a bank failure never touches the
    rendiciones, it is reported in ``bank_errors``.
    """

    def __init__(
        self,
        service: RendicionService,
        colmena: ColmenaClient,
        chubb: PortalClient,
        *,
        dry_run: bool = False,
        workflow: Workflow = DEFAULT_WORKFLOW,
        bank: BankClient | None = None,
        bank_settings: BankSettings | None = None,
    ) -> None:
        self.bank = bank
        self.bank_settings = bank_settings or BankSettings()
        self._bank_cache: list[BankMovement] | None = None
        self._bank_failed = False
        self.service = service
        self.repo = service.repo
        self.colmena = colmena
        self.chubb = chubb
        self.dry_run = dry_run
        self.workflow = workflow
        self._expired: set[str] = set()

    def run_once(self) -> PipelineReport:
        """One pass: each workflow stage submits then polls, in order; then stray polls."""
        report = PipelineReport()
        self._expired = set()
        self._bank_cache, self._bank_failed = None, False
        steps: dict[str, tuple[Step, Step]] = {
            Portal.COLMENA.value: (self.submit_ready, self.poll_colmena),
            Portal.CHUBB.value: (self.submit_chubb, self.poll_chubb),
        }
        for key in self.workflow.accounts:
            if ACCOUNTS[key].insurer is not None:
                if not self.dry_run:
                    self.verify_abonos(report, key)
                continue
            submit, poll = steps[key]
            submit(report)
            if not self.dry_run:
                poll(report)
        if not self.dry_run:
            for key, (_, poll) in steps.items():
                if not self.workflow.includes(key):
                    poll(report)
        report.sessions_expired = sorted(self._expired)
        logger.info("pipeline run: %s", {k: len(v) for k, v in report.as_dict().items()})
        return report

    def _skip(self, report: PipelineReport, portal: Portal, rendicion_id: int) -> bool:
        if portal.value in self._expired:
            report.skipped.append(rendicion_id)
            return True
        return False

    def _handle(
        self, report: PipelineReport, portal: Portal, rendicion_id: int, prefix: str, exc: Exception
    ) -> None:
        if isinstance(exc, SessionExpiredError):
            self._expired.add(portal.value)
        self._fail(report, rendicion_id, error_text(prefix, exc))

    def _dry_run_result(
        self, report: PipelineReport, portal: Portal, rendicion_id: int, result: SubmissionResult
    ) -> None:
        report.dry_run.append(rendicion_id)
        detail = {"portal": portal.value, "ok": result.ok, "message": result.message}
        self.repo.add_event(rendicion_id, "dry_run", detail)
        logger.info("rendicion %d dry-run on %s finished", rendicion_id, portal.value)

    def _fail(self, report: PipelineReport, rendicion_id: int, message: str) -> None:
        report.errors.append(rendicion_id)
        try:
            self.service.set_status(rendicion_id, Status.ERROR, error_message=message)
        except InvalidTransitionError:
            logger.warning("rendicion %d could not be moved to error", rendicion_id)
        logger.warning("rendicion %d failed in pipeline", rendicion_id)

    def submit_ready(self, report: PipelineReport) -> None:
        """Submit every rendición in ``lista`` to Colmena."""
        for rendicion in self.repo.list_rendiciones(Status.LISTA):
            if self._skip(report, Portal.COLMENA, rendicion.id):
                continue
            try:
                result = self.colmena.submit(rendicion)
                if self.dry_run:
                    self._dry_run_result(report, Portal.COLMENA, rendicion.id, result)
                    continue
                if not result.ok or not result.request_id:
                    self._fail(report, rendicion.id, result.message or "Envío a Colmena falló")
                    continue
                record_submission(
                    self.service, rendicion.id, Portal.COLMENA, result.request_id, result.raw
                )
                report.colmena_submitted.append(rendicion.id)
            except Exception as exc:
                if self.dry_run:
                    report.errors.append(rendicion.id)
                    self.repo.add_event(rendicion.id, "dry_run", {"error": error_text("", exc)})
                    continue
                self._handle(report, Portal.COLMENA, rendicion.id, "Envío a Colmena", exc)

    def poll_colmena(self, report: PipelineReport) -> None:
        """Check Colmena requests; on approval download and attach the comprobante."""
        for rendicion in self.repo.list_rendiciones(Status.COLMENA_ENVIADA):
            request_id = rendicion.colmena_request_id
            if not request_id:
                self._fail(report, rendicion.id, "Falta el N° de solicitud de Colmena")
                continue
            if self._skip(report, Portal.COLMENA, rendicion.id):
                continue
            try:
                status = self.colmena.check_status(request_id)
                if status.state == PortalState.PENDING:
                    report.pending.append(rendicion.id)
                    continue
                if status.state == PortalState.REJECTED:
                    self._record(rendicion.id, Portal.COLMENA, Outcome.REJECTED, status)
                    report.colmena_rejected.append(rendicion.id)
                    continue
                self._record(rendicion.id, Portal.COLMENA, Outcome.APPROVED, status)
                report.colmena_approved.append(rendicion.id)
                self._attach_bonificacion(rendicion.id, request_id)
            except Exception as exc:
                self._handle(report, Portal.COLMENA, rendicion.id, "Consulta a Colmena", exc)

    def _record(
        self, rendicion_id: int, portal: Portal, outcome: Outcome, status: StatusResult
    ) -> None:
        detail: dict[str, Any] = {"message": status.message, "raw": status.raw}
        record_result(self.service, rendicion_id, portal, outcome, status.monto, detail)

    def _attach_bonificacion(self, rendicion_id: int, request_id: str) -> None:
        with tempfile.TemporaryDirectory(dir=self.service.settings.tmp_dir) as tmp:
            paths = self.colmena.download_bonificaciones(request_id, Path(tmp))
            resolved = [path.resolve() for path in paths]
            if not resolved or any(Path(tmp).resolve() not in p.parents for p in resolved):
                raise ValueError("download outside destination")
            for path in resolved:
                self.service.attach_file(
                    rendicion_id, path, path.name, DocRole.BONIFICACION_COLMENA
                )

    def submit_chubb(self, report: PipelineReport) -> None:
        """Submit approved Colmena rendiciones (with comprobante) to Chubb."""
        for rendicion in self.repo.list_rendiciones(Status.COLMENA_APROBADA):
            if self._skip(report, Portal.CHUBB, rendicion.id):
                continue
            try:
                if rendicion.bonificacion_document is None:
                    if not rendicion.colmena_request_id or self.dry_run:
                        continue
                    if Portal.COLMENA.value in self._expired:
                        report.skipped.append(rendicion.id)
                        continue
                    try:
                        self._attach_bonificacion(rendicion.id, rendicion.colmena_request_id)
                    except SessionExpiredError:
                        self._expired.add(Portal.COLMENA.value)
                        raise
                    rendicion = self.repo.get_rendicion(rendicion.id)
                result = self.chubb.submit(rendicion)
                if self.dry_run:
                    self._dry_run_result(report, Portal.CHUBB, rendicion.id, result)
                    continue
                if not result.ok or not result.request_id:
                    self._fail(report, rendicion.id, result.message or "Envío a Chubb falló")
                    continue
                record_submission(
                    self.service, rendicion.id, Portal.CHUBB, result.request_id, result.raw
                )
                report.chubb_submitted.append(rendicion.id)
            except Exception as exc:
                if self.dry_run:
                    report.errors.append(rendicion.id)
                    self.repo.add_event(rendicion.id, "dry_run", {"error": error_text("", exc)})
                    continue
                portal = (
                    Portal.COLMENA
                    if isinstance(exc, SessionExpiredError) and exc.portal == "colmena"
                    else Portal.CHUBB
                )
                self._handle(report, portal, rendicion.id, "Envío a Chubb", exc)

    def poll_chubb(self, report: PipelineReport) -> None:
        """Check Chubb requests and record the final outcome."""
        for rendicion in self.repo.list_rendiciones(Status.CHUBB_ENVIADA):
            request_id = rendicion.chubb_request_id
            if not request_id:
                self._fail(report, rendicion.id, "Falta el N° de solicitud de Chubb")
                continue
            if self._skip(report, Portal.CHUBB, rendicion.id):
                continue
            try:
                status = self.chubb.check_status(request_id)
                if status.state == PortalState.PENDING:
                    report.pending.append(rendicion.id)
                elif status.state == PortalState.REJECTED:
                    self._record(rendicion.id, Portal.CHUBB, Outcome.REJECTED, status)
                    report.chubb_rejected.append(rendicion.id)
                else:
                    self._record(rendicion.id, Portal.CHUBB, Outcome.APPROVED, status)
                    report.chubb_approved.append(rendicion.id)
            except Exception as exc:
                self._handle(report, Portal.CHUBB, rendicion.id, "Consulta a Chubb", exc)
        self.fetch_chubb_liquidaciones(report)

    def fetch_chubb_liquidaciones(self, report: PipelineReport) -> None:
        """Attach Chubb's liquidación to recently resolved rendiciones that lack it.

        The PDF may show up after the decision, so a miss is retried on later runs for
        ``LIQUIDACION_WINDOW``. A failure here never changes the rendición's status.
        """
        if not isinstance(self.chubb, LiquidacionProvider):
            return
        cutoff = utcnow() - LIQUIDACION_WINDOW
        for status in (Status.CHUBB_APROBADA, Status.CHUBB_RECHAZADA):
            for rendicion in self.repo.list_rendiciones(status):
                request_id = rendicion.chubb_request_id
                resolved = rendicion.chubb_resolved_at
                if (
                    not request_id
                    or rendicion.chubb_response_document is not None
                    or (resolved is not None and resolved < cutoff)
                ):
                    continue
                if Portal.CHUBB.value in self._expired:
                    return
                try:
                    if self._attach_liquidacion(self.chubb, rendicion.id, request_id):
                        report.chubb_liquidaciones.append(rendicion.id)
                except SessionExpiredError:
                    self._expired.add(Portal.CHUBB.value)
                    return
                except Exception as exc:
                    logger.warning(
                        "rendicion %d: chubb liquidación failed (%s)",
                        rendicion.id,
                        type(exc).__name__,
                    )

    def _attach_liquidacion(
        self, client: LiquidacionProvider, rendicion_id: int, request_id: str
    ) -> bool:
        with tempfile.TemporaryDirectory(dir=self.service.settings.tmp_dir) as tmp:
            path = client.download_liquidacion(request_id, Path(tmp))
            if path is None:
                return False
            resolved = path.resolve()
            if Path(tmp).resolve() not in resolved.parents:
                raise ValueError("download outside destination")
            self.service.attach_file(rendicion_id, resolved, resolved.name, DocRole.RESPUESTA_CHUBB)
        return True

    # ------------------------------------------------------------------ bank checks

    def _bank_credits(self, since: date) -> list[BankMovement] | None:
        """Every wanted abono in the cartola, read once per run (None when unavailable)."""
        if self._bank_cache is not None or self._bank_failed:
            return self._bank_cache
        if self.bank is None:
            self._bank_failed = True
            raise BankError("No hay cliente de banco configurado")
        patterns = [p for v in self.bank_settings.glosas.values() for p in v]
        try:
            self._bank_cache = self.bank.credits(since, lambda d: glosa_matches(d, patterns))
        except Exception:
            self._bank_failed = True
            raise
        return self._bank_cache

    def verify_abonos(self, report: PipelineReport, account: str) -> None:
        """Confirm in the bank the transfers of the insurer behind ``account``."""
        spec = ACCOUNTS[account]
        insurer = spec.insurer
        assert insurer is not None
        today = utcnow().date()
        waiting: dict[int, tuple[Expected, date]] = {}
        excedentes: dict[int, int] = {}
        for rendicion in self.repo.list_rendiciones():
            payment = rendicion.bank_payments.get(account)
            if payment is not None and payment.state == ABONADO:
                continue
            monto = expected_transfer(insurer, rendicion)
            if not monto:
                continue
            submitted = ACCOUNTS[insurer].submitted_at(rendicion)
            resolved = ACCOUNTS[insurer].resolved_at(rendicion) or submitted
            if submitted is None or resolved is None:
                continue
            deadline = resolved.date() + timedelta(days=self.bank_settings.wait_days)
            waiting[rendicion.id] = (Expected(rendicion.id, monto, submitted.date()), deadline)
            excedentes[rendicion.id] = monto - (approved_monto(insurer, rendicion) or 0)
        if not waiting:
            return
        since = min(e.earliest for e, _ in waiting.values())
        try:
            movements = self._bank_credits(since)
        except Exception as exc:
            message = str(exc) if isinstance(exc, BankError) else type(exc).__name__
            if message not in report.bank_errors:
                report.bank_errors.append(message)
            logger.warning("bank read failed: %s", type(exc).__name__)
            return
        if movements is None:
            return
        matches = match_transfers(
            [e for e, _ in waiting.values()],
            movements,
            self.bank_settings.patterns(insurer),
            self.repo.used_movement_keys(),
        )
        for match in matches:
            for expected in match.expected:
                self.repo.set_bank_payment(
                    expected.rendicion_id,
                    account,
                    ABONADO,
                    expected.monto,
                    movement_key=match.movement.key,
                    operation_id=match.movement.operation_id,
                    movement_amount=match.movement.amount,
                    movement_date=match.movement.occurred_on,
                    excedente=excedentes[expected.rendicion_id],
                )
                report.abonos_confirmed.append(expected.rendicion_id)
                waiting.pop(expected.rendicion_id)
        for rendicion_id, (expected, deadline) in waiting.items():
            if deadline < today:
                current = self.repo.get_rendicion(rendicion_id).bank_payments.get(account)
                if current is None:
                    self.repo.set_bank_payment(rendicion_id, account, NO_LLEGO, expected.monto)
                report.abonos_missing.append(rendicion_id)
            else:
                report.abonos_waiting.append(rendicion_id)
