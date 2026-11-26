"""Pipeline passes started from the web UI, run in a background thread."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from rendiciones.automation.base import ColmenaClient, PortalClient
from rendiciones.automation.loader import FAKE_SPEC, PLAYWRIGHT_SPEC, load_clients, resolve_spec
from rendiciones.bank import BankClient
from rendiciones.bank.settings import BankSettingsStore
from rendiciones.config import Settings
from rendiciones.db import Repository
from rendiciones.locking import AutomationLock, Lease
from rendiciones.pipeline import Pipeline
from rendiciones.services import RendicionService
from rendiciones.workflow import DEFAULT_WORKFLOW, WorkflowStore

logger = logging.getLogger(__name__)

ClientsFactory = Callable[[bool], tuple[ColmenaClient, PortalClient]]
BankFactory = Callable[[], BankClient]


class RunMode(StrEnum):
    """``dry_run`` fills the forms without submitting; ``submit`` sends for real."""

    DRY_RUN = "dry_run"
    SUBMIT = "submit"


class RunState(StrEnum):
    """State of the last pass started from the UI."""

    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"


@dataclass
class RunJob:
    """One pass: report holds rendición ids only."""

    mode: RunMode
    state: RunState
    started_at: str
    finished_at: str | None = None
    message: str = ""
    report: dict[str, list[Any]] | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def clients_factory(spec: str | None = None) -> ClientsFactory:
    """Portal clients for ``spec`` (alias, ``module:function`` or env); Playwright by default."""
    resolved = resolve_spec(spec) or PLAYWRIGHT_SPEC

    def build(dry_run: bool) -> tuple[ColmenaClient, PortalClient]:
        if resolved == PLAYWRIGHT_SPEC:
            return load_clients(resolved, dry_run=dry_run)
        return load_clients(resolved)

    return build


def bank_factory(spec: str | None, settings: Settings) -> BankFactory | None:
    """Bank client matching the portal clients: fake for demos, Banco de Chile otherwise."""
    resolved = resolve_spec(spec) or PLAYWRIGHT_SPEC

    def build() -> BankClient:
        if resolved == FAKE_SPEC:
            from rendiciones.bank.fake import PayingFakeBank

            bank_settings = BankSettingsStore.for_data_dir(settings.data_dir).read()
            return PayingFakeBank(Repository(settings.db_path), bank_settings)
        from rendiciones.bank.bancochile import bank_client

        return bank_client(settings)

    return build if resolved in (FAKE_SPEC, PLAYWRIGHT_SPEC) else None


def _close(*clients: object) -> None:
    for client in clients:
        close = getattr(client, "close", None)
        if callable(close):
            close()


class PipelineRunner:
    """Starts at most one pass at a time; the automation lock also excludes the CLI."""

    def __init__(
        self,
        service: RendicionService,
        lock: AutomationLock,
        factory: ClientsFactory,
        workflows: WorkflowStore | None = None,
        bank: BankFactory | None = None,
    ) -> None:
        self.bank = bank
        self.service = service
        self.lock = lock
        self.factory = factory
        self.workflows = workflows
        self._job: RunJob | None = None
        self._guard = threading.Lock()

    def status(self) -> dict[str, Any]:
        """Last pass started here plus who holds the browser right now."""
        return {
            "job": asdict(self._job) if self._job else None,
            "busy_by": self.lock.holder(),
        }

    def start(self, mode: RunMode) -> dict[str, Any]:
        """Start a pass in the background or raise ``AutomationBusyError``."""
        with self._guard:
            lease = self.lock.acquire(f"envío desde la app ({mode.value})")
            self._job = RunJob(mode=mode, state=RunState.RUNNING, started_at=_now())
        threading.Thread(target=self._run, args=(self._job, lease), daemon=True).start()
        return self.status()

    def _run(self, job: RunJob, lease: Lease) -> None:
        dry_run = job.mode is RunMode.DRY_RUN
        clients: tuple[object, ...] = ()
        state, message, report = RunState.FAILED, "", None
        try:
            colmena, chubb = self.factory(dry_run)
            clients = (colmena, chubb)
            workflow = self.workflows.read() if self.workflows else DEFAULT_WORKFLOW
            bank_settings = BankSettingsStore.for_data_dir(self.service.settings.data_dir).read()
            pipeline = Pipeline(
                self.service,
                colmena,
                chubb,
                dry_run=dry_run,
                workflow=workflow,
                bank=self.bank() if self.bank else None,
                bank_settings=bank_settings,
            )
            report = pipeline.run_once().as_dict()
            state, message = RunState.OK, "Pasada terminada"
        except Exception as exc:
            logger.warning("pipeline run from the UI failed: %s", type(exc).__name__)
            message = f"La pasada falló ({type(exc).__name__}): {exc}"[:300]
        finally:
            try:
                _close(*clients)
            finally:
                lease.release()
                job.report, job.message, job.finished_at = report, message, _now()
                job.state = state
