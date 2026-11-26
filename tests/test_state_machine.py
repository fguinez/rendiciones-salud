"""Tests for the rendición state machine and its persistence."""

from __future__ import annotations

import pytest

from rendiciones.db import Repository
from rendiciones.domain import TRANSITIONS, InvalidTransitionError, Status, check_transition
from rendiciones.services import RendicionService

ALLOWED = [(src, dst) for src, targets in TRANSITIONS.items() for dst in targets]
FORBIDDEN = [
    (Status.BORRADOR, Status.COLMENA_ENVIADA),
    (Status.BORRADOR, Status.CHUBB_ENVIADA),
    (Status.LISTA, Status.COLMENA_APROBADA),
    (Status.COLMENA_ENVIADA, Status.CHUBB_ENVIADA),
    (Status.COLMENA_RECHAZADA, Status.CHUBB_ENVIADA),
    (Status.CHUBB_APROBADA, Status.BORRADOR),
    (Status.ERROR, Status.ERROR),
]


@pytest.mark.parametrize(("current", "target"), ALLOWED)
def test_allowed_transitions(current: Status, target: Status) -> None:
    """Every transition declared in TRANSITIONS is accepted."""
    check_transition(current, target)


@pytest.mark.parametrize(("current", "target"), FORBIDDEN)
def test_forbidden_transitions(current: Status, target: Status) -> None:
    """Skipping steps or leaving terminal states is rejected."""
    with pytest.raises(InvalidTransitionError):
        check_transition(current, target)


@pytest.mark.parametrize("current", [s for s in Status if s != Status.ERROR])
def test_any_state_can_fail(current: Status) -> None:
    """Every non-error state may move to ``error``."""
    check_transition(current, Status.ERROR)


@pytest.mark.parametrize("target", [Status.COLMENA_ENVIADA, Status.BORRADOR])
def test_error_recovery_allowed(target: Status) -> None:
    """From ``error`` the previous state or ``borrador`` are reachable."""
    previous = Status.COLMENA_ENVIADA

    check_transition(Status.ERROR, target, previous)


@pytest.mark.parametrize("target", [Status.CHUBB_ENVIADA, Status.LISTA])
def test_error_recovery_forbidden(target: Status) -> None:
    """From ``error`` any other state is rejected."""
    previous = Status.COLMENA_ENVIADA

    with pytest.raises(InvalidTransitionError):
        check_transition(Status.ERROR, target, previous)


def test_repository_transition_records_history(service: RendicionService) -> None:
    """Applied transitions are persisted and logged as events."""
    repo: Repository = service.repo
    rid = repo.create_rendicion(confidence=1.0, reason="test")

    repo.transition(rid, Status.LISTA)
    view = repo.transition(rid, Status.ERROR, updates={"error_message": "falló"})

    assert view.status == Status.ERROR
    assert view.status_before_error == Status.LISTA
    assert view.error_message == "falló"
    kinds = [(e.from_status, e.to_status) for e in repo.events(rid)]
    assert kinds == [
        (None, Status.BORRADOR),
        (Status.BORRADOR, Status.LISTA),
        (Status.LISTA, Status.ERROR),
    ]


def test_repository_recovers_from_error(service: RendicionService) -> None:
    """Returning from ``error`` clears the error fields."""
    repo = service.repo
    rid = repo.create_rendicion(confidence=1.0, reason="test")
    repo.transition(rid, Status.LISTA)
    repo.transition(rid, Status.ERROR, updates={"error_message": "falló"})

    view = repo.transition(rid, Status.LISTA)

    assert view.status == Status.LISTA
    assert view.error_message is None
    assert view.status_before_error is None


def test_repository_rejects_invalid_transition(service: RendicionService) -> None:
    """Invalid transitions leave the stored status unchanged."""
    rid = service.repo.create_rendicion(confidence=1.0, reason="test")

    with pytest.raises(InvalidTransitionError):
        service.repo.transition(rid, Status.CHUBB_APROBADA)

    assert service.repo.get_rendicion(rid).status == Status.BORRADOR
