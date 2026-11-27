"""Bank checks: cartola parsing, transfer matching, settings and the abono workflow nodes.

All cartola rows are synthetic (fake account numbers, ids and amounts).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture

from rendiciones.automation.fake import FakeColmenaClient, FakePortalClient
from rendiciones.bank import BankError, BankMovement
from rendiciones.bank import cartola as ct
from rendiciones.bank.fake import FakeBankClient
from rendiciones.bank.matching import Expected, glosa_matches, match_transfers
from rendiciones.bank.settings import BankSettings, BankSettingsStore, validate
from rendiciones.services import RendicionService
from rendiciones.workflow import Workflow, check, progress

COLMENA_GLOSA = "Pago:proveedores 0762966190"
CHUBB_GLOSA = "Pago:proveedores 0774088202"
FULL_FLOW = Workflow(stages=(("colmena",), ("chubb", "abono_colmena"), ("abono_chubb",)))
TODAY = datetime.now(UTC).date()


def row(**overrides: Any) -> dict[str, Any]:
    """A synthetic ``getCartola`` abono row."""
    base: dict[str, Any] = {
        "id": "001-0000000-20260110-28000-A",
        "monto": "28.000",
        "tipo": "abono",
        "descripcion": f"  {COLMENA_GLOSA}  ",
        "fecha": "20260110 12:00:00",
        "fechaContable": "10/01/2026",
        "saldo": "500.000",
        "numeroCuenta": "000000000",
        "infoDataGlosaAdicional": "token",
        "detalleGlosa": [],
    }
    return base | overrides


def movement(amount: int, on: date, glosa: str = COLMENA_GLOSA, key: str = "") -> BankMovement:
    """A parsed abono."""
    return BankMovement(key or f"k{amount}{on}", None, on, amount, glosa)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$1.234.567", 1234567),
        ("28.000", 28000),
        ("1.234,60", 1235),
        (5460, 5460),
        ("", None),
        (True, None),
    ],
)
def test_parse_clp(raw: object, expected: int | None) -> None:
    """Chilean-formatted amounts become whole pesos."""
    assert ct.parse_clp(raw) == expected


def test_parse_credit_reads_an_abono() -> None:
    """An abono row keeps its date, amount, glosa and a stable key."""
    parsed = ct.parse_credit(row(), "10000000001")

    assert parsed == BankMovement(
        key="fp:001-0000000-20260110-28000-A|500.000",
        operation_id="10000000001",
        occurred_on=date(2026, 1, 10),
        amount=28000,
        description=COLMENA_GLOSA,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"tipo": "cargo"},
        {"monto": "x"},
        {"fecha": "", "fechaContable": ""},
        {"id": "", "saldo": ""},
    ],
)
def test_parse_credit_skips_unusable_rows(overrides: dict[str, Any]) -> None:
    """Cargos and rows without amount, date or identity are not abonos."""
    assert ct.parse_credit(row(**overrides)) is None


@pytest.mark.parametrize(
    ("glosa", "expected"),
    [
        (["Fecha: 10/01/2026", "Id transacción: 10000000001"], "10000000001"),
        (["Id transaccion - 0000"], None),
        ([], None),
        (None, None),
    ],
)
def test_operation_id_from_glosa(glosa: object, expected: str | None) -> None:
    """The inline "ID Transacción" is read when present and not a placeholder."""
    assert ct.operation_id_from_glosa(glosa) == expected


@pytest.mark.parametrize(
    ("payload", "body", "expected"),
    [
        ({"moneda": "CLP"}, None, True),
        ({}, {"cuentaSeleccionada": {"moneda": "USD"}}, False),
        ({"moneda": "USD"}, None, False),
        ({}, None, True),
    ],
)
def test_is_clp(payload: dict[str, Any], body: dict[str, Any] | None, expected: bool) -> None:
    """A cartola for another currency is never read as the CLP one."""
    assert ct.is_clp(payload, body) is expected


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("PAGO:PROVEEDORES   0762966190", True),
        ("Pago:proveedores 0774088202", False),
        ("Traspaso de: Fulano", False),
    ],
)
def test_glosa_matches(description: str, expected: bool) -> None:
    """Glosas match ignoring case and spacing."""
    assert glosa_matches(description, [COLMENA_GLOSA, ""]) is expected


def test_match_single_transfer() -> None:
    """A transfer pays the rendición whose approved amount it equals."""
    expected = [Expected(1, 28000, date(2026, 1, 1)), Expected(2, 5000, date(2026, 1, 1))]

    matches = match_transfers(expected, [movement(28000, date(2026, 1, 10))], [COLMENA_GLOSA])

    assert [[e.rendicion_id for e in m.expected] for m in matches] == [[1]]


def test_match_transfer_paying_several_rendiciones() -> None:
    """One transfer may pay several approved rendiciones whose amounts add up to it."""
    expected = [
        Expected(1, 3000, date(2026, 1, 1)),
        Expected(2, 2460, date(2026, 1, 2)),
        Expected(3, 9999, date(2026, 1, 2)),
    ]

    matches = match_transfers(
        expected, [movement(5460, date(2026, 1, 12), CHUBB_GLOSA)], [CHUBB_GLOSA]
    )

    assert [sorted(e.rendicion_id for e in m.expected) for m in matches] == [[1, 2]]


@pytest.mark.parametrize(
    ("transfer", "used"),
    [
        (movement(28000, date(2026, 1, 10), CHUBB_GLOSA), frozenset()),
        (movement(28000, date(2025, 12, 1)), frozenset()),
        (movement(28001, date(2026, 1, 10)), frozenset()),
        (movement(28000, date(2026, 1, 10), key="used"), frozenset({"used"})),
    ],
)
def test_match_rejects(transfer: BankMovement, used: frozenset[str]) -> None:
    """Other insurers' glosas, earlier dates, other amounts and used transfers never match."""
    expected = [Expected(1, 28000, date(2026, 1, 1))]

    assert match_transfers(expected, [transfer], [COLMENA_GLOSA], used) == []


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"glosas": {"colmena": ["a"], "chubb": "b"}, "wait_days": 10}, None),
        ({"glosas": {}, "wait_days": 0}, "días de espera"),
        ({"glosas": {"colmena": ["x" * 121]}, "wait_days": 10}, "demasiado larga"),
        ({"wait_days": 10}, "Faltan las glosas"),
    ],
)
def test_validate_settings(data: dict[str, Any], error: str | None) -> None:
    """User-entered settings are trimmed and bounded."""
    if error is None:
        assert validate(data).glosas == {"colmena": ("a",), "chubb": ("b",)}
    else:
        with pytest.raises(ValueError, match=error):
            validate(data)


def test_settings_default_to_the_known_glosas(tmp_path: Any) -> None:
    """Without a saved file, Colmena's and Chubb's known glosas are searched."""
    settings = BankSettingsStore(tmp_path / "bank.json").read()

    assert settings.patterns("colmena") == (COLMENA_GLOSA,)
    assert settings.patterns("chubb") == (CHUBB_GLOSA,)


@pytest.mark.parametrize(
    ("stages", "problem"),
    [
        ([["colmena"], ["chubb", "abono_colmena"], ["abono_chubb"]], None),
        ([["colmena", "abono_colmena"]], "Abono Colmena necesita la aprobación de Colmena"),
        ([["colmena"], ["abono_chubb"]], "Abono Chubb necesita la aprobación de Chubb"),
    ],
)
def test_abono_nodes_need_the_insurer_before(stages: list[list[str]], problem: str | None) -> None:
    """Each abono node needs its insurer's approval in an earlier stage."""
    problems = check(stages)

    assert problems == ([] if problem is None else [f"{problem} en una etapa anterior"])


def run(
    service: RendicionService, bank: FakeBankClient, workflow: Workflow = FULL_FLOW
) -> dict[str, list[Any]]:
    """One pipeline pass with fake portals approving 70% of the amount."""
    from rendiciones.pipeline import Pipeline

    pipeline = Pipeline(
        service,
        FakeColmenaClient(),
        FakePortalClient("chubb"),
        workflow=workflow,
        bank=bank,
        bank_settings=BankSettings(),
    )
    return pipeline.run_once().as_dict()


def node_states(service: RendicionService, rid: int) -> dict[str, str]:
    """Progress state per account."""
    view = service.repo.get_rendicion(rid)
    return {n["account"]: n["state_label"] for n in progress(view, FULL_FLOW)}


def test_pipeline_confirms_both_abonos(service: RendicionService, lista_rendicion: int) -> None:
    """With both transfers in the cartola, both abono nodes end as "Abonado"."""
    bank = FakeBankClient(
        [
            movement(16800, TODAY, COLMENA_GLOSA, "c"),
            movement(16800, TODAY, CHUBB_GLOSA, "h"),
        ]
    )

    report = run(service, bank)

    assert report["abonos_confirmed"] == [lista_rendicion, lista_rendicion]
    assert bank.reads == 1
    states = node_states(service, lista_rendicion)
    assert states["abono_colmena"] == states["abono_chubb"] == "Abonado"


def test_pipeline_shows_colmena_excedente(
    service: RendicionService, lista_rendicion: int, mocker: MockerFixture
) -> None:
    """A Colmena transfer that also pays "Uso Excedente" shows that part apart."""
    mocker.patch("rendiciones.bank.expected.colmena_excedente", return_value=7200)
    bank = FakeBankClient([movement(24000, TODAY, COLMENA_GLOSA, "c")])

    run(service, bank)

    view = service.repo.get_rendicion(lista_rendicion)
    node = next(n for n in progress(view, FULL_FLOW) if n["account"] == "abono_colmena")
    assert node["monto"] == 24000
    assert node["breakdown"] == [
        {"label": "Bonificado", "monto": 16800},
        {"label": "Excedentes", "monto": 7200},
    ]
    events = service.repo.events(lista_rendicion)
    assert any(e.kind == "abono_abonado" and e.detail.get("excedente") == 7200 for e in events)


def test_pipeline_waits_for_missing_abono(service: RendicionService, lista_rendicion: int) -> None:
    """A transfer that has not arrived leaves the node waiting, within the wait window."""
    report = run(service, FakeBankClient([movement(16800, TODAY, COLMENA_GLOSA, "c")]))

    assert report["abonos_waiting"] == [lista_rendicion]
    assert node_states(service, lista_rendicion)["abono_chubb"] == "Esperando abono"


def test_pipeline_marks_overdue_abono(
    service: RendicionService, lista_rendicion: int, mocker: MockerFixture
) -> None:
    """Past the wait window without the transfer, the node reads "No llegó"."""
    run(service, FakeBankClient([movement(16800, TODAY, COLMENA_GLOSA, "c")]))
    later = datetime(TODAY.year + 1, 1, 1, tzinfo=UTC)
    mocker.patch("rendiciones.pipeline.utcnow", return_value=later)

    report = run(service, FakeBankClient())

    assert report["abonos_missing"] == [lista_rendicion]
    assert node_states(service, lista_rendicion)["abono_chubb"] == "No llegó"


def test_bank_failure_is_reported_not_fatal(
    service: RendicionService, lista_rendicion: int
) -> None:
    """A bank error is reported once and leaves the rendición's state untouched."""
    bank = FakeBankClient(fail=BankError("Guarda la cuenta del Banco de Chile en Cuentas"))

    report = run(service, bank)

    assert report["bank_errors"] == ["Guarda la cuenta del Banco de Chile en Cuentas"]
    assert report["errors"] == []
    assert bank.reads == 1


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://portalpersonas.bancochile.cl/mibancochile-web/front/persona/index.html", True),
        (
            "https://login.portales.bancochile.cl/login?redirect_uri="
            "https%3A%2F%2Fportalpersonas.bancochile.cl%3A443%2Fmibancochile-web%2Fredirect_uri",
            False,
        ),
        ("about:blank", False),
    ],
)
def test_on_portal_checks_the_host(url: str, expected: bool, mocker: MockerFixture) -> None:
    """An expired session's login page does not count as the portal."""
    from rendiciones.bank.bancochile import _on_portal

    page = mocker.MagicMock(url=url)

    assert _on_portal(page) is expected


def test_bancochile_reads_while_a_portal_session_is_open(mocker: MockerFixture) -> None:
    """The bank starts its own Playwright even when the caller's thread already runs one."""
    from contextlib import contextmanager

    from playwright.sync_api import sync_playwright

    from rendiciones.bank.bancochile import BancoChileClient
    from rendiciones.credentials import Credential, MemoryStore

    @contextmanager
    def session(_self: object, _credential: object) -> Any:
        with sync_playwright():
            yield None

    store = MemoryStore()
    store.set("bancochile", Credential("11111111-1", "clave"))
    mocker.patch.object(BancoChileClient, "_session", session)
    mocker.patch.object(BancoChileClient, "_read_cartolas", return_value=[])
    client = BancoChileClient(store, session_file=mocker.MagicMock(), channel="")
    portal = sync_playwright().start()

    try:
        found = client.credits(date(2026, 1, 1), lambda _: True)
    finally:
        portal.stop()

    assert found == []


def test_bank_is_not_read_without_abono_nodes(
    service: RendicionService, lista_rendicion: int
) -> None:
    """Without abono nodes in the workflow, the bank is never consulted."""
    bank = FakeBankClient()

    run(service, bank, Workflow(stages=(("colmena",), ("chubb",))))

    assert bank.reads == 0


def test_bank_settings_api(client: TestClient) -> None:
    """The glosas can be read and changed from the UI."""
    body = {"glosas": {"colmena": ["Otra glosa"], "chubb": []}, "wait_days": 15}

    saved = client.put("/api/bank/settings", json=body)

    assert saved.json() == body
    assert client.get("/api/bank/settings").json()["wait_days"] == 15


def test_colmena_excedente_adds_every_comprobante(
    service: RendicionService, lista_rendicion: int, mocker: MockerFixture
) -> None:
    """With several comprobantes, the excedentes Colmena pays is their sum."""
    from rendiciones.bank.expected import colmena_excedente
    from rendiciones.pipeline import Pipeline

    pipeline = Pipeline(service, FakeColmenaClient(comprobantes=2), FakePortalClient("chubb"))
    pipeline.run_once()
    mocker.patch(
        "rendiciones.bank.expected.pdf_text",
        side_effect=["Uso Excedente 1.500", "Uso Excedente 12.000"],
    )

    total = colmena_excedente(service.repo.get_rendicion(lista_rendicion))

    assert total == 13500


def test_colmena_excedente_is_unknown_when_a_comprobante_is_unreadable(
    service: RendicionService, lista_rendicion: int, mocker: MockerFixture
) -> None:
    """One unreadable comprobante makes the excedente unknown (0), never a partial sum."""
    from rendiciones.bank.expected import colmena_excedente
    from rendiciones.pipeline import Pipeline

    pipeline = Pipeline(service, FakeColmenaClient(comprobantes=2), FakePortalClient("chubb"))
    pipeline.run_once()
    mocker.patch(
        "rendiciones.bank.expected.pdf_text",
        side_effect=["Uso Excedente 1.500", ValueError("unreadable")],
    )

    total = colmena_excedente(service.repo.get_rendicion(lista_rendicion))

    assert total == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Totales 40.000 28.000\nUso Excedente 12.000\nFolio Excedente 12345678", 12000),
        ("Totales 40.000 28.000\nUso Excedente 1.500", 1500),
        ("Totales 40.000 28.000\nForma de Pago: Transferencia", 0),
    ],
)
def test_uso_excedente(text: str, expected: int) -> None:
    """Colmena's transfer adds the "Uso Excedente" amount; its folio is never read as one."""
    from rendiciones.bank.expected import uso_excedente

    assert uso_excedente(text) == expected
