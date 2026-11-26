"""Tests for the editable workflow: validation, storage, progress and the pipeline order."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rendiciones.automation.fake import FakeColmenaClient, FakePortalClient
from rendiciones.domain import Status
from rendiciones.pipeline import Pipeline
from rendiciones.services import RendicionService
from rendiciones.workflow import (
    DEFAULT_WORKFLOW,
    Workflow,
    WorkflowStore,
    build,
    check,
    progress,
)

COLMENA_ONLY = Workflow(stages=(("colmena",),))


@pytest.mark.parametrize(
    ("stages", "problem"),
    [
        ([["colmena"], ["chubb"]], None),
        ([["colmena"], [], ["chubb"]], None),
        ([["colmena"]], None),
        ([["chubb"], ["colmena"]], "Chubb necesita el comprobante de una Isapre"),
        ([["colmena", "chubb"]], "Chubb necesita el comprobante de una Isapre"),
        ([], "al menos una cuenta"),
        ([["colmena"], ["colmena"]], "más de una vez"),
        ([["banco"]], "Cuenta desconocida"),
    ],
)
def test_check(stages: list[list[str]], problem: str | None) -> None:
    """Orders the engine cannot run are explained; runnable ones pass."""
    problems = check(stages)

    if problem is None:
        assert problems == []
    else:
        assert any(problem in p for p in problems)


def test_store_round_trip_and_reset(tmp_path: Path) -> None:
    """A saved workflow is read back; reset returns to the default."""
    store = WorkflowStore.for_data_dir(tmp_path)

    store.save(build([["colmena"]]))
    saved = store.read()
    store.reset()

    assert saved == COLMENA_ONLY
    assert store.read() == DEFAULT_WORKFLOW
    assert (tmp_path / "workflow.json").exists() is False


def test_store_falls_back_to_default_when_invalid(tmp_path: Path) -> None:
    """A file holding an order that no longer validates is ignored."""
    (tmp_path / "workflow.json").write_text('{"stages": [["chubb"]]}', encoding="utf-8")

    assert WorkflowStore.for_data_dir(tmp_path).read() == DEFAULT_WORKFLOW


@pytest.mark.parametrize(
    ("status", "before", "expected"),
    [
        (Status.BORRADOR, None, ["pendiente", "pendiente"]),
        (Status.LISTA, None, ["por_enviar", "pendiente"]),
        (Status.COLMENA_ENVIADA, None, ["en_revision", "pendiente"]),
        (Status.COLMENA_APROBADA, None, ["aprobada", "por_enviar"]),
        (Status.COLMENA_RECHAZADA, None, ["rechazada", "no_continua"]),
        (Status.CHUBB_ENVIADA, None, ["aprobada", "en_revision"]),
        (Status.CHUBB_APROBADA, None, ["aprobada", "aprobada"]),
        (Status.ERROR, Status.LISTA, ["error", "pendiente"]),
        (Status.ERROR, Status.COLMENA_APROBADA, ["aprobada", "error"]),
    ],
)
def test_progress_states(
    service: RendicionService,
    lista_rendicion: int,
    status: Status,
    before: Status | None,
    expected: list[str],
) -> None:
    """Each account's dot follows the rendición status, and an error lands on the active one."""
    view = replace(
        service.repo.get_rendicion(lista_rendicion), status=status, status_before_error=before
    )

    nodes = progress(view, DEFAULT_WORKFLOW)

    assert [n["state"] for n in nodes] == expected


def test_progress_shows_account_outside_workflow_when_used(
    service: RendicionService, lista_rendicion: int
) -> None:
    """An account removed from the workflow still shows when the rendición was sent there."""
    view = replace(
        service.repo.get_rendicion(lista_rendicion),
        status=Status.CHUBB_ENVIADA,
        chubb_request_id="CH-1",
    )

    nodes = progress(view, COLMENA_ONLY)

    assert [(n["account"], n["in_workflow"]) for n in nodes] == [
        ("colmena", True),
        ("chubb", False),
    ]


def test_progress_shows_failed_account_outside_workflow(
    service: RendicionService, lista_rendicion: int
) -> None:
    """An error on an account removed from the workflow keeps that account visible."""
    view = replace(
        service.repo.get_rendicion(lista_rendicion),
        status=Status.ERROR,
        status_before_error=Status.COLMENA_APROBADA,
    )

    nodes = progress(view, COLMENA_ONLY)

    assert [(n["account"], n["state"]) for n in nodes] == [
        ("colmena", "aprobada"),
        ("chubb", "error"),
    ]


def test_pipeline_skips_accounts_outside_workflow(
    service: RendicionService, lista_rendicion: int
) -> None:
    """With only Colmena in the workflow, approved rendiciones are not sent to Chubb."""
    chubb = FakePortalClient("chubb")

    Pipeline(service, FakeColmenaClient(), chubb, workflow=COLMENA_ONLY).run_once()

    assert service.repo.get_rendicion(lista_rendicion).status == Status.COLMENA_APROBADA
    assert chubb.submitted == []


def test_api_rejects_unrunnable_workflow(client: TestClient) -> None:
    """PUT returns 422 with the problems and keeps the saved workflow."""
    response = client.put("/api/workflow", json={"stages": [["chubb"], ["colmena"]]})

    assert response.status_code == 422
    assert "Chubb" in response.json()["detail"][0]
    assert client.get("/api/workflow").json()["stages"] == [["colmena"], ["chubb"]]


def test_api_saves_workflow_and_progress_follows(client: TestClient, lista_rendicion: int) -> None:
    """A saved workflow drives the progress nodes of every rendición."""
    client.put("/api/workflow", json={"stages": [["colmena"]]})

    rendicion = client.get(f"/api/rendiciones/{lista_rendicion}").json()

    assert [n["account"] for n in rendicion["progress"]] == ["colmena"]
    assert rendicion["progress"][0]["state_label"] == "Por enviar"
