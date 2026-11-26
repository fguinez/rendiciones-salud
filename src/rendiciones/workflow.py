"""Rendición workflow: which portal accounts a rendición goes through, and in what order.

A workflow is a list of stages; the accounts of a stage run side by side and each stage
starts once the previous one is done. Accounts declare what they ``require`` from an
earlier stage and what they ``provide`` to later ones (Chubb needs Colmena's comprobante),
so an order the engine cannot run is rejected before it is saved.

The optional ``abono_*`` nodes check the bank for the transfer of an insurer's approved
reimbursement; each needs that insurer's approval in an earlier stage.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from rendiciones.config import FILE_MODE
from rendiciones.domain import RendicionView, Status

logger = logging.getLogger(__name__)

WORKFLOW_FILE = "workflow.json"
COMPROBANTE_ISAPRE = "comprobante_isapre"
APROBACION_COLMENA = "aprobacion_colmena"
APROBACION_CHUBB = "aprobacion_chubb"
BANK_CREDENTIAL = "bancochile"
ABONADO = "abonado"
NO_LLEGO = "no_llego"


class NodeState(StrEnum):
    """Progress of one account for one rendición."""

    PENDIENTE = "pendiente"
    POR_ENVIAR = "por_enviar"
    EN_REVISION = "en_revision"
    APROBADA = "aprobada"
    RECHAZADA = "rechazada"
    ERROR = "error"
    NO_CONTINUA = "no_continua"


NODE_LABELS = {
    NodeState.PENDIENTE: "Pendiente",
    NodeState.POR_ENVIAR: "Por enviar",
    NodeState.EN_REVISION: "En revisión",
    NodeState.APROBADA: "Aprobada",
    NodeState.RECHAZADA: "Rechazada",
    NodeState.ERROR: "Error",
    NodeState.NO_CONTINUA: "No continúa",
}
ACTIVE_STATES = frozenset({NodeState.POR_ENVIAR, NodeState.EN_REVISION})


@dataclass(frozen=True)
class AccountSpec:
    """A portal account the engine can submit to."""

    key: str
    label: str
    kind: str
    description: str
    provides: frozenset[str]
    requires: frozenset[str]
    monto_label: str
    states: dict[Status, NodeState]
    monto: Callable[[RendicionView], int | None]
    request_id: Callable[[RendicionView], str | None]
    submitted_at: Callable[[RendicionView], datetime | None]
    resolved_at: Callable[[RendicionView], datetime | None]
    credential: str = ""
    insurer: str | None = None
    state_fn: Callable[[RendicionView], NodeState] | None = None
    labels: dict[NodeState, str] = field(default_factory=dict)
    breakdown: Callable[[RendicionView], list[tuple[str, int]]] = lambda r: []

    def state_of(self, rendicion: RendicionView) -> NodeState:
        """This account's progress for ``rendicion``."""
        if self.state_fn is not None:
            return self.state_fn(rendicion)
        if rendicion.status != Status.ERROR:
            return self.states[rendicion.status]
        before = rendicion.status_before_error
        if before is None:
            return NodeState.ERROR
        state = self.states[before]
        return NodeState.ERROR if state in ACTIVE_STATES else state

    def label_of(self, state: NodeState) -> str:
        """How this account names ``state``."""
        return self.labels.get(state, NODE_LABELS[state])


def approved_monto(insurer: str, rendicion: RendicionView) -> int | None:
    """What ``insurer`` approved for ``rendicion`` (None until it approves)."""
    spec = ACCOUNTS[insurer]
    if spec.state_of(rendicion) != NodeState.APROBADA:
        return None
    return spec.monto(rendicion) or 0


def _abono_state(account: str, insurer: str) -> Callable[[RendicionView], NodeState]:
    def state(rendicion: RendicionView) -> NodeState:
        payment = rendicion.bank_payments.get(account)
        if payment is not None and payment.state == ABONADO:
            return NodeState.APROBADA
        insurer_state = ACCOUNTS[insurer].state_of(rendicion)
        if insurer_state in (NodeState.RECHAZADA, NodeState.NO_CONTINUA):
            return NodeState.NO_CONTINUA
        if insurer_state != NodeState.APROBADA:
            return NodeState.PENDIENTE
        if not ACCOUNTS[insurer].monto(rendicion):
            return NodeState.NO_CONTINUA
        if payment is not None and payment.state == NO_LLEGO:
            return NodeState.RECHAZADA
        return NodeState.EN_REVISION

    return state


def _abono_spec(
    insurer: str, label: str, requirement: str, extra_label: str | None = None
) -> AccountSpec:
    """``extra_label`` names what a transfer may bring beyond the insurer's approved amount."""
    key = f"abono_{insurer}"

    def paid(rendicion: RendicionView) -> int | None:
        payment = rendicion.bank_payments.get(key)
        return payment.monto if payment and payment.state == ABONADO else None

    def operation(rendicion: RendicionView) -> str | None:
        payment = rendicion.bank_payments.get(key)
        return payment.operation_id if payment and payment.state == ABONADO else None

    def arrived(rendicion: RendicionView) -> datetime | None:
        payment = rendicion.bank_payments.get(key)
        if payment is None or payment.state != ABONADO or payment.movement_date is None:
            return None
        return datetime.combine(payment.movement_date, datetime.min.time())

    def breakdown(rendicion: RendicionView) -> list[tuple[str, int]]:
        approved = ACCOUNTS[insurer].monto(rendicion) or 0
        total = paid(rendicion) or 0
        if extra_label is None or not approved or total <= approved:
            return []
        return [(ACCOUNTS[insurer].monto_label, approved), (extra_label, total - approved)]

    return AccountSpec(
        key=key,
        label=f"Abono {label}",
        kind="Banco de Chile",
        description=f"Confirma en la cartola que llegó la transferencia de {label}.",
        provides=frozenset(),
        requires=frozenset({requirement}),
        monto_label="Abonado",
        states={},
        monto=paid,
        request_id=operation,
        submitted_at=lambda r: None,
        resolved_at=arrived,
        credential=BANK_CREDENTIAL,
        insurer=insurer,
        state_fn=_abono_state(key, insurer),
        labels={
            NodeState.EN_REVISION: "Esperando abono",
            NodeState.APROBADA: "Abonado",
            NodeState.RECHAZADA: "No llegó",
            NodeState.NO_CONTINUA: "Sin abono",
        },
        breakdown=breakdown,
    )


ACCOUNTS: dict[str, AccountSpec] = {
    "colmena": AccountSpec(
        key="colmena",
        label="Colmena",
        kind="Isapre",
        description="Recibe la rendición primero y entrega el comprobante de bonificación.",
        provides=frozenset({COMPROBANTE_ISAPRE, APROBACION_COLMENA}),
        requires=frozenset(),
        monto_label="Bonificado",
        states={
            Status.BORRADOR: NodeState.PENDIENTE,
            Status.LISTA: NodeState.POR_ENVIAR,
            Status.COLMENA_ENVIADA: NodeState.EN_REVISION,
            Status.COLMENA_APROBADA: NodeState.APROBADA,
            Status.COLMENA_RECHAZADA: NodeState.RECHAZADA,
            Status.CHUBB_ENVIADA: NodeState.APROBADA,
            Status.CHUBB_APROBADA: NodeState.APROBADA,
            Status.CHUBB_RECHAZADA: NodeState.APROBADA,
        },
        monto=lambda r: r.colmena_monto_bonificado,
        request_id=lambda r: r.colmena_request_id,
        submitted_at=lambda r: r.colmena_submitted_at,
        resolved_at=lambda r: r.colmena_resolved_at,
    ),
    "chubb": AccountSpec(
        key="chubb",
        label="Chubb",
        kind="Seguro complementario",
        description="Reembolsa el saldo; necesita el comprobante de la Isapre.",
        provides=frozenset({APROBACION_CHUBB}),
        requires=frozenset({COMPROBANTE_ISAPRE}),
        monto_label="Reembolsado",
        states={
            Status.BORRADOR: NodeState.PENDIENTE,
            Status.LISTA: NodeState.PENDIENTE,
            Status.COLMENA_ENVIADA: NodeState.PENDIENTE,
            Status.COLMENA_APROBADA: NodeState.POR_ENVIAR,
            Status.COLMENA_RECHAZADA: NodeState.NO_CONTINUA,
            Status.CHUBB_ENVIADA: NodeState.EN_REVISION,
            Status.CHUBB_APROBADA: NodeState.APROBADA,
            Status.CHUBB_RECHAZADA: NodeState.RECHAZADA,
        },
        monto=lambda r: r.chubb_monto_reembolsado,
        request_id=lambda r: r.chubb_request_id,
        submitted_at=lambda r: r.chubb_submitted_at,
        resolved_at=lambda r: r.chubb_resolved_at,
    ),
    "abono_colmena": _abono_spec("colmena", "Colmena", APROBACION_COLMENA, "Excedentes"),
    "abono_chubb": _abono_spec("chubb", "Chubb", APROBACION_CHUBB),
}

REQUIREMENT_LABELS = {
    COMPROBANTE_ISAPRE: "el comprobante de una Isapre",
    APROBACION_COLMENA: "la aprobación de Colmena",
    APROBACION_CHUBB: "la aprobación de Chubb",
}


class WorkflowError(ValueError):
    """The requested workflow cannot be run; ``problems`` explains why."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Workflow:
    """Ordered stages of account keys (validated)."""

    stages: tuple[tuple[str, ...], ...]

    @property
    def accounts(self) -> tuple[str, ...]:
        """Every account in run order."""
        return tuple(key for stage in self.stages for key in stage)

    def includes(self, key: str) -> bool:
        """True when ``key`` takes part in the workflow."""
        return key in self.accounts

    def stage_of(self, key: str) -> int | None:
        """Index of the stage holding ``key``."""
        for index, stage in enumerate(self.stages):
            if key in stage:
                return index
        return None

    def as_json(self) -> list[list[str]]:
        """Plain lists for JSON."""
        return [list(stage) for stage in self.stages]


DEFAULT_WORKFLOW = Workflow(stages=(("colmena",), ("chubb",)))


def normalize(stages: Sequence[Sequence[str]]) -> tuple[tuple[str, ...], ...]:
    """Drop empty stages."""
    return tuple(tuple(stage) for stage in stages if stage)


def check(stages: Sequence[Sequence[str]]) -> list[str]:
    """Problems that keep ``stages`` from running (empty when valid)."""
    normalized = normalize(stages)
    problems: list[str] = []
    seen: set[str] = set()
    for stage in normalized:
        for key in stage:
            if key not in ACCOUNTS:
                problems.append(f"Cuenta desconocida: {key}")
            elif key in seen:
                problems.append(f"{ACCOUNTS[key].label} aparece más de una vez")
            seen.add(key)
    if not seen:
        problems.append("El flujo necesita al menos una cuenta")
    if problems:
        return problems
    provided: set[str] = set()
    for stage in normalized:
        for key in stage:
            spec = ACCOUNTS[key]
            for need in sorted(spec.requires - provided):
                problems.append(
                    f"{spec.label} necesita {REQUIREMENT_LABELS.get(need, need)} "
                    "en una etapa anterior"
                )
        for key in stage:
            provided |= ACCOUNTS[key].provides
    return problems


def build(stages: Sequence[Sequence[str]]) -> Workflow:
    """Validated workflow or ``WorkflowError``."""
    problems = check(stages)
    if problems:
        raise WorkflowError(problems)
    return Workflow(stages=normalize(stages))


class WorkflowStore:
    """The saved workflow, as 0600 JSON in the data directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> WorkflowStore:
        """Store at ``<data_dir>/workflow.json``."""
        return cls(data_dir / WORKFLOW_FILE)

    def read(self) -> Workflow:
        """Saved workflow, or the default when missing or no longer valid."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return build(data["stages"])
        except FileNotFoundError:
            return DEFAULT_WORKFLOW
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("saved workflow is unreadable; using the default")
            return DEFAULT_WORKFLOW

    def save(self, workflow: Workflow) -> Workflow:
        """Persist ``workflow`` atomically."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"stages": workflow.as_json()}, fh)
            tmp.replace(self.path)
        logger.info("workflow saved: %d stages", len(workflow.stages))
        return workflow

    def reset(self) -> Workflow:
        """Go back to the default workflow."""
        self.path.unlink(missing_ok=True)
        return DEFAULT_WORKFLOW


def account_json(spec: AccountSpec) -> dict[str, Any]:
    """Account metadata for the workflow editor."""
    return {
        "key": spec.key,
        "label": spec.label,
        "kind": spec.kind,
        "description": spec.description,
        "credential": spec.credential or spec.key,
        "requires": [REQUIREMENT_LABELS.get(r, r) for r in sorted(spec.requires)],
        "provides": [REQUIREMENT_LABELS.get(p, p) for p in sorted(spec.provides)],
    }


def progress(rendicion: RendicionView, workflow: Workflow) -> list[dict[str, Any]]:
    """One node per account: workflow accounts in order, then others it used or failed on."""

    def iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    keys = list(workflow.accounts)
    keys += [
        k
        for k, spec in ACCOUNTS.items()
        if k not in keys
        and (spec.request_id(rendicion) or spec.state_of(rendicion) == NodeState.ERROR)
    ]
    nodes = []
    for key in keys:
        spec = ACCOUNTS[key]
        state = spec.state_of(rendicion)
        stage = workflow.stage_of(key)
        nodes.append(
            {
                "account": key,
                "label": spec.label,
                "stage": stage,
                "in_workflow": stage is not None,
                "state": state.value,
                "state_label": spec.label_of(state),
                "request_id": spec.request_id(rendicion),
                "monto": spec.monto(rendicion),
                "monto_label": spec.monto_label,
                "breakdown": [
                    {"label": label, "monto": monto} for label, monto in spec.breakdown(rendicion)
                ],
                "submitted_at": iso(spec.submitted_at(rendicion)),
                "resolved_at": iso(spec.resolved_at(rendicion)),
            }
        )
    return nodes
