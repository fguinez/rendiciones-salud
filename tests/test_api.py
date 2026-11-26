"""Tests for the REST API: uploads, security checks and automation report-back."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import PdfFactory
from rendiciones.config import CSRF_HEADER

PDF = "application/pdf"


def upload(
    client: TestClient,
    files: list[tuple[str, bytes]],
    paths: list[str] | None = None,
    **headers: str,
) -> Any:
    """POST /api/uploads with multipart files and optional relative paths."""
    response = client.post(
        "/api/uploads",
        files=[("files", (name, data, PDF)) for name, data in files],
        data={"paths": paths or []},
        headers=headers or None,
    )
    return response


def test_upload_groups_and_reports_per_file(client: TestClient, pdf_of: PdfFactory) -> None:
    """Uploading related files returns per-file results and groups them."""
    files = [
        ("boleta.pdf", pdf_of("boleta_laboratorio")),
        ("orden.pdf", pdf_of("orden_medica")),
        ("receta.pdf", pdf_of("receta")),
    ]

    response = upload(client, files)

    assert response.status_code == 200
    body = response.json()
    assert [f["ok"] for f in body["files"]] == [True, True, True]
    assert [f["doc_type"] for f in body["files"]] == [
        "boleta_electronica",
        "orden_medica",
        "receta",
    ]
    assert len(body["grouping"]["created"]) == 2
    assert body["files"][0]["rendicion_id"] == body["files"][1]["rendicion_id"]


def test_upload_rejects_path_traversal(client: TestClient, pdf_of: PdfFactory) -> None:
    """A relative path with ``..`` is rejected and nothing is stored."""
    files = [("boleta.pdf", pdf_of("boleta_laboratorio"))]

    response = upload(client, files, paths=["../../etc/boleta.pdf"])

    result = response.json()["files"][0]
    assert response.status_code == 200
    assert not result["ok"]
    assert "Ruta inválida" in result["error"]
    assert client.get("/api/rendiciones").json() == []


def test_upload_sanitizes_filename(client: TestClient, pdf_of: PdfFactory) -> None:
    """Directory components in the filename itself are stripped."""
    files = [("../../../tmp/evil.pdf", pdf_of("receta"))]

    response = upload(client, files)

    assert response.json()["files"][0]["name"] == "evil.pdf"


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("boleta.pdf", b"<html><script>alert(1)</script></html>"),
        ("foto.png", b"GIF89a....."),
        ("vacio.pdf", b""),
    ],
)
def test_upload_rejects_bad_type(client: TestClient, name: str, data: bytes) -> None:
    """Files whose magic bytes are not PDF/JPG/PNG/HEIC/WEBP are rejected."""
    response = upload(client, [(name, data)])

    result = response.json()["files"][0]
    assert not result["ok"]
    assert result["error"]


def test_upload_dedupes(client: TestClient, pdf_of: PdfFactory) -> None:
    """The same file uploaded twice is reported as a duplicate of the first document."""
    data = pdf_of("boleta_honorarios")
    first = upload(client, [("a.pdf", data)]).json()["files"][0]

    second = upload(client, [("b.pdf", data)]).json()

    assert second["files"][0]["duplicate"]
    assert second["files"][0]["document_id"] == first["document_id"]
    assert len(second["duplicates"]) == 1
    assert len(client.get("/api/rendiciones").json()) == 1


def test_upload_size_limit(client: TestClient) -> None:
    """Files over the configured limit are rejected."""
    client.app.state.service.store.max_bytes = 100  # type: ignore[attr-defined]

    response = upload(client, [("big.pdf", b"%PDF-1.4\n" + b"0" * 200)])

    assert "máximo" in response.json()["files"][0]["error"]


def test_folder_upload_groups_by_subfolder(client: TestClient, pdf_of: PdfFactory) -> None:
    """Unrelated documents in the same uploaded sub-folder end up together."""
    files = [("lentes.pdf", pdf_of("factura_lentes")), ("otro.pdf", pdf_of("otro"))]

    response = upload(
        client, files, paths=["Septiembre/Optica/lentes.pdf", "Septiembre/Optica/otro.pdf"]
    )

    rendiciones = client.get("/api/rendiciones").json()
    assert response.status_code == 200
    assert len(rendiciones) == 1
    assert rendiciones[0]["grouping_reason"] == "misma carpeta"


def test_mutation_without_csrf_header_is_rejected(client: TestClient) -> None:
    """Mutating requests without the custom header get 403."""
    response = client.post("/api/batches/abc/group", headers={CSRF_HEADER: ""})

    assert response.status_code == 403


def test_cross_origin_mutation_is_rejected(client: TestClient) -> None:
    """Mutating requests from a foreign Origin get 403."""
    response = client.post("/api/batches/abc/group", headers={"Origin": "https://evil.example"})

    assert response.status_code == 403


def test_foreign_host_is_rejected(client: TestClient) -> None:
    """Requests with a non-local Host header are refused (DNS rebinding)."""
    response = client.get("/api/health", headers={"Host": "evil.example"})

    assert response.status_code == 400


def test_index_is_served_with_security_headers(client: TestClient) -> None:
    """The UI is served with a restrictive CSP."""
    response = client.get("/")

    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"


@pytest.fixture
def lista_id(client: TestClient, pdf_of: PdfFactory) -> int:
    """Upload a boleta + orden and mark the resulting rendición as ``lista``."""
    files = [("boleta.pdf", pdf_of("boleta_laboratorio")), ("orden.pdf", pdf_of("orden_medica"))]
    rid = int(upload(client, files).json()["grouping"]["created"][0])
    client.post(f"/api/rendiciones/{rid}/status", json={"status": "lista"}).raise_for_status()
    return rid


def test_list_ready_includes_absolute_paths(client: TestClient, lista_id: int) -> None:
    """An external agent can list ``lista`` rendiciones with absolute file paths."""
    response = client.get("/api/rendiciones", params={"status": "lista"})

    body = response.json()
    assert [r["id"] for r in body] == [lista_id]
    paths = [Path(d["path"]) for d in body[0]["documents"]]
    assert all(p.is_absolute() and p.exists() for p in paths)


def test_agent_report_back_flow(client: TestClient, lista_id: int, pdf_of: PdfFactory) -> None:
    """Submissions, results and the comprobante can be reported through the API."""
    base = f"/api/rendiciones/{lista_id}"
    client.post(f"{base}/submissions", json={"portal": "colmena", "request_id": "C-1"})
    client.post(f"{base}/results", json={"portal": "colmena", "outcome": "approved", "monto": 9})
    early = client.post(f"{base}/submissions", json={"portal": "chubb", "request_id": "X-1"})
    attach = client.post(
        f"{base}/documents",
        files={"file": ("comprobante.pdf", pdf_of("comprobante_bonificacion"), PDF)},
        data={"role": "bonificacion_colmena"},
    )

    response = client.post(f"{base}/submissions", json={"portal": "chubb", "request_id": "X-1"})

    assert early.status_code == 409
    assert attach.status_code == 200
    assert attach.json()["role"] == "bonificacion_colmena"
    body = response.json()
    assert body["status"] == "chubb_enviada"
    assert body["colmena_monto_bonificado"] == 9
    assert body["bonificacion_document_id"] == attach.json()["id"]
    events = client.get(f"{base}/events").json()
    assert [e["kind"] for e in events][-1] == "chubb_enviada"


def test_invalid_status_change_is_conflict(client: TestClient, lista_id: int) -> None:
    """Skipping states via the API returns 409."""
    response = client.post(f"/api/rendiciones/{lista_id}/status", json={"status": "chubb_aprobada"})

    assert response.status_code == 409


def test_patch_and_move(client: TestClient, lista_id: int) -> None:
    """Fields can be edited and documents moved to a new rendición."""
    doc_id = client.get(f"/api/rendiciones/{lista_id}").json()["documents"][1]["id"]

    patched = client.patch(f"/api/rendiciones/{lista_id}", json={"monto": 12345})
    moved = client.post(f"/api/documents/{doc_id}/move", json={"rendicion_id": None})

    assert patched.json()["monto"] == 12345
    assert patched.json()["fields_locked"]
    assert moved.json()["id"] != lista_id
    assert [d["id"] for d in moved.json()["documents"]] == [doc_id]


def test_document_file_is_served(client: TestClient, lista_id: int) -> None:
    """Stored files can be viewed inline."""
    doc_id = client.get(f"/api/rendiciones/{lista_id}").json()["documents"][0]["id"]

    response = client.get(f"/api/documents/{doc_id}/file")

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]


def test_unknown_rendicion_is_404(client: TestClient) -> None:
    """Unknown ids return 404."""
    response = client.get("/api/rendiciones/999")

    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/", "/static/app.js"])
def test_ui_assets_are_revalidated(client: TestClient, path: str) -> None:
    """The browser must revalidate the UI so a new version is never mixed with a cached one."""
    assert client.get(path).headers["Cache-Control"] == "no-cache"


def test_patch_document_recomputes_rendicion(client: TestClient, lista_id: int) -> None:
    """Correcting a document returns it with the edited fields and updates the rendición."""
    doc_id = client.get(f"/api/rendiciones/{lista_id}").json()["documents"][0]["id"]

    response = client.patch(
        f"/api/documents/{doc_id}", json={"monto": 30000, "provider_rut": "76.123.456-0"}
    )

    body = response.json()
    assert response.status_code == 200
    assert (body["monto"], body["provider_rut"]) == (30000, "76123456-0")
    assert body["edited_fields"] == ["monto", "provider_rut"]
    assert client.get(f"/api/rendiciones/{lista_id}").json()["monto"] == 30000


def test_patch_document_null_clears_field(client: TestClient, lista_id: int) -> None:
    """An explicit null clears a field and omitted fields keep their value."""
    doc = client.get(f"/api/rendiciones/{lista_id}").json()["documents"][0]

    body = client.patch(f"/api/documents/{doc['id']}", json={"folio": None}).json()

    assert body["folio"] is None
    assert body["monto"] == doc["monto"]
    assert body["edited_fields"] == ["folio"]


@pytest.mark.parametrize(
    "payload",
    [
        {"monto": -1},
        {"doc_type": None},
        {"doc_type": "pagare"},
        {"provider_rut": "11.111.111-2"},
        {"fecha": "ayer"},
        {"montos": 1},
    ],
)
def test_patch_document_rejects_invalid_values(
    client: TestClient, lista_id: int, payload: dict[str, Any]
) -> None:
    """Invalid values, a null doc_type and unknown fields are rejected."""
    doc_id = client.get(f"/api/rendiciones/{lista_id}").json()["documents"][0]["id"]

    response = client.patch(f"/api/documents/{doc_id}", json=payload)

    assert response.status_code == 422


def test_patch_unknown_document_is_404(client: TestClient) -> None:
    """Editing a document that does not exist returns 404."""
    response = client.patch("/api/documents/999", json={"monto": 1})

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [("PATCH", "/api/documents/{doc_id}"), ("POST", "/api/rendiciones/{rid}/recompute")],
)
def test_edits_of_sent_rendicion_are_conflict(
    client: TestClient, lista_id: int, method: str, path: str
) -> None:
    """A rendición already sent to a portal rejects document edits and recomputes."""
    doc_id = client.get(f"/api/rendiciones/{lista_id}").json()["documents"][0]["id"]
    submission = {"portal": "colmena", "request_id": "C-1"}
    client.post(f"/api/rendiciones/{lista_id}/submissions", json=submission).raise_for_status()

    response = client.request(method, path.format(doc_id=doc_id, rid=lista_id), json={"monto": 1})

    assert response.status_code == 409


def test_recompute_unlocks_fields(client: TestClient, lista_id: int) -> None:
    """Recompute discards manual values and goes back to the documents' totals."""
    client.patch(f"/api/rendiciones/{lista_id}", json={"monto": 1}).raise_for_status()

    response = client.post(f"/api/rendiciones/{lista_id}/recompute")

    body = response.json()
    assert response.status_code == 200
    assert body["monto"] == 24000
    assert not body["fields_locked"]


def test_restore_grouping_sets_confidence_and_reason(client: TestClient, lista_id: int) -> None:
    """The grouping a move replaced can be put back through the API."""
    body = {"confidence": 0.6, "reason": "documento único"}

    response = client.put(f"/api/rendiciones/{lista_id}/grouping", json=body)

    view = response.json()
    assert response.status_code == 200
    assert (view["grouping_confidence"], view["grouping_reason"]) == (0.6, "documento único")


def test_restore_grouping_rejects_confidence_above_one(client: TestClient, lista_id: int) -> None:
    """A grouping confidence outside 0..1 is a validation error."""
    body = {"confidence": 1.5, "reason": "x"}

    response = client.put(f"/api/rendiciones/{lista_id}/grouping", json=body)

    assert response.status_code == 422


@pytest.fixture
def uploaded(client: TestClient, pdf_of: PdfFactory) -> list[dict[str, Any]]:
    """Per-file results of uploading a lab boleta + orden and an unrelated factura."""
    files = [
        ("boleta.pdf", pdf_of("boleta_laboratorio")),
        ("orden.pdf", pdf_of("orden_medica")),
        ("lentes.pdf", pdf_of("factura_lentes")),
    ]
    return list(upload(client, files).json()["files"])


def test_bulk_move_to_existing_rendicion(
    client: TestClient, uploaded: list[dict[str, Any]]
) -> None:
    """Moving every document of a rendición into another merges them and deletes the source."""
    boleta, _, lentes = uploaded
    payload = {"document_ids": [lentes["document_id"]], "rendicion_id": boleta["rendicion_id"]}

    response = client.post("/api/documents/move", json=payload)

    docs = client.get(f"/api/rendiciones/{boleta['rendicion_id']}").json()["documents"]
    assert response.json() == {
        "rendicion_id": boleta["rendicion_id"],
        "deleted_rendiciones": [lentes["rendicion_id"]],
    }
    assert [d["id"] for d in docs] == [f["document_id"] for f in uploaded]


def test_bulk_move_to_new_rendicion(client: TestClient, uploaded: list[dict[str, Any]]) -> None:
    """A null target creates a single new rendición and deletes the sources left empty."""
    boleta, orden, lentes = uploaded
    ids = [orden["document_id"], lentes["document_id"]]

    response = client.post("/api/documents/move", json={"document_ids": ids, "rendicion_id": None})

    body = response.json()
    remaining = {
        r["id"]: [d["id"] for d in r["documents"]] for r in client.get("/api/rendiciones").json()
    }
    assert body["deleted_rendiciones"] == [lentes["rendicion_id"]]
    assert remaining == {boleta["rendicion_id"]: [boleta["document_id"]], body["rendicion_id"]: ids}


@pytest.mark.parametrize("sent_is_target", [False, True])
def test_bulk_move_rejects_sent_rendicion(
    client: TestClient, uploaded: list[dict[str, Any]], sent_is_target: bool
) -> None:
    """Moving out of or into a rendición already sent to a portal is a 409 and moves nothing."""
    boleta, _, lentes = uploaded
    sent = boleta["rendicion_id"]
    client.post(f"/api/rendiciones/{sent}/status", json={"status": "lista"}).raise_for_status()
    submission = {"portal": "colmena", "request_id": "C-1"}
    client.post(f"/api/rendiciones/{sent}/submissions", json=submission).raise_for_status()
    doc, target = (
        (lentes["document_id"], sent)
        if sent_is_target
        else (boleta["document_id"], lentes["rendicion_id"])
    )

    response = client.post(
        "/api/documents/move", json={"document_ids": [doc], "rendicion_id": target}
    )

    docs = client.get(f"/api/rendiciones/{lentes['rendicion_id']}").json()["documents"]
    assert response.status_code == 409
    assert [d["id"] for d in docs] == [lentes["document_id"]]


@pytest.mark.parametrize("payload", [{"document_ids": []}, {"rendicion_id": None}])
def test_bulk_move_requires_documents(client: TestClient, payload: dict[str, Any]) -> None:
    """A bulk move without documents is rejected."""
    response = client.post("/api/documents/move", json=payload)

    assert response.status_code == 422
