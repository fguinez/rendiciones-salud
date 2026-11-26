"""Tests for the rule-based classifier, the LLM payload parser and classifier selection."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_mock import MockerFixture

from conftest import fixture_text, render_pdf
from rendiciones.domain import DocType, Extraction, TipoPrestacion
from rendiciones.recognition import factory
from rendiciones.recognition.base import RecognitionInput
from rendiciones.recognition.llm import AnthropicClassifier, parse_llm_payload
from rendiciones.recognition.rules import (
    RuleBasedClassifier,
    extract_amount,
    extract_from_text,
    find_dates,
    normalize_text,
    parse_amount,
)
from rendiciones.recognition.text import extract_text


@pytest.mark.parametrize(
    ("name", "doc_type", "provider_rut", "patient_rut", "fecha", "monto", "folio", "tipo"),
    [
        (
            "boleta_laboratorio",
            DocType.BOLETA_ELECTRONICA,
            "76123456-0",
            "11111111-1",
            date(2026, 10, 3),
            24000,
            "981",
            TipoPrestacion.EXAMENES_LABORATORIO,
        ),
        (
            "boleta_honorarios",
            DocType.BOLETA_HONORARIOS,
            "22333444-K",
            "11111111-1",
            date(2026, 10, 14),
            45000,
            "4521",
            TipoPrestacion.OTROS_PROFESIONALES,
        ),
        (
            "boleta_honorarios_sii",
            DocType.BOLETA_HONORARIOS,
            "22333444-K",
            "11111111-1",
            date(2026, 1, 5),
            40000,
            "900",
            TipoPrestacion.OTROS_PROFESIONALES,
        ),
        (
            "factura_lentes",
            DocType.FACTURA,
            "77654321-7",
            None,
            date(2026, 9, 5),
            89990,
            "1234",
            TipoPrestacion.LENTES,
        ),
        (
            "orden_medica",
            DocType.ORDEN_MEDICA,
            "77654321-7",
            "11111111-1",
            date(2026, 10, 1),
            None,
            None,
            TipoPrestacion.EXAMENES_LABORATORIO,
        ),
        (
            "receta",
            DocType.RECETA,
            "22333444-K",
            "15555555-6",
            date(2026, 9, 20),
            None,
            None,
            TipoPrestacion.MEDICAMENTOS,
        ),
        (
            "programa_medico",
            DocType.PROGRAMA_MEDICO,
            "77654321-7",
            "11111111-1",
            date(2026, 8, 15),
            1250000,
            "777",
            TipoPrestacion.HOSPITALIZACION,
        ),
        (
            "informe_examen",
            DocType.INFORME_EXAMEN,
            "76123456-0",
            None,
            date(2026, 7, 2),
            None,
            None,
            TipoPrestacion.IMAGENOLOGIA,
        ),
        (
            "comprobante_bonificacion",
            DocType.COMPROBANTE_BONIFICACION,
            "76123456-0",
            "11111111-1",
            date(2026, 10, 10),
            24000,
            "981",
            TipoPrestacion.EXAMENES_LABORATORIO,
        ),
        (
            "documento_chubb",
            DocType.DOCUMENTO_CHUBB,
            None,
            "11111111-1",
            date(2026, 10, 30),
            5040,
            None,
            None,
        ),
        ("otro", DocType.OTRO, None, None, None, None, None, None),
    ],
)
def test_rules_extract_fixture(
    name: str,
    doc_type: DocType,
    provider_rut: str | None,
    patient_rut: str | None,
    fecha: date | None,
    monto: int | None,
    folio: str | None,
    tipo: TipoPrestacion | None,
) -> None:
    """Each synthetic fixture is classified with the expected fields."""
    text = fixture_text(name)

    result = extract_from_text(text)

    assert result.doc_type == doc_type
    assert result.provider_rut == provider_rut
    assert result.patient_rut == patient_rut
    assert result.fecha == fecha
    assert result.monto == monto
    assert result.folio == folio
    assert result.tipo_prestacion == tipo
    assert result.classifier == "rules"


def test_rules_extract_bonificado_amount() -> None:
    """The comprobante de bonificación exposes the bonified amount as an extra."""
    text = fixture_text("comprobante_bonificacion")

    result = extract_from_text(text)

    assert result.extras["monto_bonificado"] == 16800


@pytest.mark.parametrize("name", ["boleta_honorarios", "boleta_honorarios_sii"])
def test_rules_extract_honorarios_glosa(name: str) -> None:
    """The 'Por atención profesional' glosa is kept as an extra, without its amount."""
    text = fixture_text(name)

    result = extract_from_text(text)

    assert result.extras["descripcion"] == "Sesión de psicoterapia"


def test_rules_provider_name_from_header() -> None:
    """The issuer name is taken from the line next to its RUT."""
    text = fixture_text("boleta_laboratorio")

    result = extract_from_text(text)

    assert result.provider_name == "LABORATORIO CLÍNICO DEMO SPA"


def test_rules_empty_text_is_otro() -> None:
    """Documents without text (e.g. failed OCR) are classified as ``otro`` with 0 confidence."""
    result = extract_from_text("   \n  ")

    assert result.doc_type == DocType.OTRO
    assert result.confidence == 0.0


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("FECHA: 03/10/2026", [date(2026, 10, 3)]),
        ("FECHA: 2026-07-02", [date(2026, 7, 2)]),
        ("20 DE SEPTIEMBRE DE 2026", [date(2026, 9, 20)]),
        ("31/02/2026", []),
    ],
)
def test_find_dates(line: str, expected: list[date]) -> None:
    """Numeric, ISO and Spanish long dates are parsed; impossible dates are skipped."""
    result = find_dates(line)

    assert result == expected


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["Total Honorarios $: 40.000", "10,75% Impto. Retenido: 4.300", "Total: 35.700"], 40000),
        (["Total Honorarios: 40.000"], 40000),
        (["Total Honorarios", "$ 40.000"], 40000),
        (["Total Honorarios $:", "40.000", "Impto. Retenido:", "5.500"], 40000),
        (["Retención: $ 5.500", "Líquido a pagar: $ 34.500", "Total Honorarios $ 40.000"], 40000),
        (["Monto:", "RUT: 22.333.444-K"], None),
    ],
    ids=["sii-inline", "no-currency", "split-label", "split-with-retention", "net-first", "rut"],
)
def test_extract_amount_honorarios(lines: list[str], expected: int | None) -> None:
    """The gross Total Honorarios is the amount; retention, líquido and RUTs are ignored."""
    norm_lines = [normalize_text(line) for line in lines]

    total, _ = extract_amount(norm_lines, DocType.BOLETA_HONORARIOS)

    assert total == expected


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["Total honorarios médicos $ 300.000", "Total a pagar $ 1.200.000"], 1200000),
        (["Líquido a pagar $ 50.000"], 50000),
        (["Prestación Cant. Monto Bonificación", "Consulta 1 $ 40.000 $ 16.800"], 40000),
    ],
    ids=["honorarios-line-in-factura", "liquido", "table-header"],
)
def test_extract_amount_other_documents(lines: list[str], expected: int) -> None:
    """Outside boletas de honorarios the largest total wins and líquido still counts."""
    norm_lines = [normalize_text(line) for line in lines]

    total, _ = extract_amount(norm_lines, DocType.FACTURA)

    assert total == expected


def test_extract_amount_honorarios_and_retention_on_one_line() -> None:
    """A flattened line keeps the gross amount before the retention label."""
    norm_lines = [normalize_text("Total Honorarios: $ 40.000   Retención: $ 5.500")]

    total, _ = extract_amount(norm_lines, DocType.BOLETA_HONORARIOS)

    assert total == 40000


def test_rules_glosa_ignores_a_following_amount_label() -> None:
    """An empty glosa does not borrow the next line when it is a total."""
    text = "BOLETA DE HONORARIOS\nPor atención profesional:\nTotal Honorarios $: 40.000"

    result = extract_from_text(text)

    assert "descripcion" not in result.extras


def test_rules_accept_unicode_minus_in_ruts() -> None:
    """RUTs written with a typographic minus sign (as in SII PDFs) are recognised."""
    text = "BOLETA DE HONORARIOS\nRUT: 22.333.444\u2212K\nSeñor(es): RUT: 11.111.111\u2212 1"

    result = extract_from_text(text)

    assert result.provider_rut == "22333444-K"
    assert result.patient_rut == "11111111-1"


@pytest.mark.parametrize(("raw", "expected"), [("45.000", 45000), ("1.250.000", 1250000)])
def test_parse_amount(raw: str, expected: int) -> None:
    """CLP amounts with dot thousands separators are parsed as integers."""
    result = parse_amount(raw)

    assert result == expected


def test_extract_text_from_pdf(tmp_path: Path) -> None:
    """Embedded PDF text is extracted with pypdf, without OCR."""
    path = tmp_path / "boleta.pdf"
    path.write_bytes(render_pdf(fixture_text("boleta_laboratorio")))

    text, method = extract_text(path, "application/pdf")

    assert method == "pdf_text"
    assert "BOLETA ELECTR" in text


def test_parse_llm_payload_validates_fields() -> None:
    """Invalid RUTs and dates from the model are dropped; valid values are normalised."""
    payload: dict[str, Any] = {
        "doc_type": "boleta_electronica",
        "provider_name": "Laboratorio Demo",
        "provider_rut": "76.123.456-0",
        "patient_rut": "11.111.111-2",
        "fecha": "2026-13-40",
        "monto_total": 24000,
        "monto_bonificado": None,
        "folio": "000981",
        "tipo_prestacion": "examenes_laboratorio",
        "confidence": 1.7,
    }

    result = parse_llm_payload(payload)

    assert result.doc_type == DocType.BOLETA_ELECTRONICA
    assert result.provider_rut == "76123456-0"
    assert result.patient_rut is None
    assert result.fecha is None
    assert result.folio == "981"
    assert result.confidence == 1.0
    assert result.classifier == "llm"


def test_parse_llm_payload_keeps_descripcion() -> None:
    """The service description returned by the model is stored as an extra."""
    payload: dict[str, Any] = {"doc_type": "boleta_honorarios", "descripcion": " Psicoterapia "}

    result = parse_llm_payload(payload)

    assert result.extras["descripcion"] == "Psicoterapia"


def test_parse_llm_payload_unknown_enum_values() -> None:
    """Unknown doc types and prestaciones fall back to safe defaults."""
    payload: dict[str, Any] = {"doc_type": "pasaporte", "tipo_prestacion": "spa"}

    result = parse_llm_payload(payload)

    assert result.doc_type == DocType.OTRO
    assert result.tipo_prestacion is None


@pytest.fixture
def pdf_input(tmp_path: Path) -> RecognitionInput:
    """A recognition input backed by a synthetic boleta PDF."""
    text = fixture_text("boleta_laboratorio")
    path = tmp_path / "doc.pdf"
    path.write_bytes(render_pdf(text))
    return RecognitionInput(path=path, mime="application/pdf", text=text)


def test_llm_classifier_uses_model_output(
    mocker: MockerFixture, pdf_input: RecognitionInput
) -> None:
    """The LLM classifier parses the JSON returned by the API."""
    payload = {"doc_type": "factura", "monto_total": 1000, "confidence": 0.9}
    api = mocker.Mock()
    api.messages.create.return_value = SimpleNamespace(
        stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(payload))]
    )
    classifier = AnthropicClassifier(api_key="test", fallback=RuleBasedClassifier(), client=api)

    result = classifier.classify(pdf_input)

    assert result.doc_type == DocType.FACTURA
    assert result.monto == 1000
    kwargs = api.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-5-5"
    assert kwargs["messages"][0]["content"][0]["type"] == "document"


def test_llm_classifier_falls_back_on_error(
    mocker: MockerFixture, pdf_input: RecognitionInput
) -> None:
    """API failures fall back to the rule-based classifier."""
    api = mocker.Mock()
    api.messages.create.side_effect = RuntimeError("boom")
    classifier = AnthropicClassifier(api_key="test", fallback=RuleBasedClassifier(), client=api)

    result = classifier.classify(pdf_input)

    assert result.classifier == "rules"
    assert result.doc_type == DocType.BOLETA_ELECTRONICA


@pytest.mark.parametrize(("mode", "key"), [("rules", "sk-test"), ("auto", None), ("llm", None)])
def test_build_classifier_uses_rules(mocker: MockerFixture, mode: str, key: str | None) -> None:
    """Without an API key (or when forced) the rule-based classifier is used."""
    mocker.patch.object(factory, "anthropic_api_key", return_value=key)

    result = factory.build_classifier(mode)

    assert result.name == "rules"


def test_build_classifier_uses_llm_with_key(mocker: MockerFixture) -> None:
    """With an API key available, ``auto`` selects the LLM classifier."""
    mocker.patch.object(factory, "anthropic_api_key", return_value="sk-test")
    mocker.patch("anthropic.Anthropic")

    result = factory.build_classifier("auto")

    assert result.name == "llm"


def test_rule_classifier_ignores_path(tmp_path: Path) -> None:
    """The rule classifier works from text only."""
    doc = RecognitionInput(path=tmp_path / "x.pdf", mime="application/pdf", text="RECETA MEDICA")

    result = RuleBasedClassifier().classify(doc)

    assert isinstance(result, Extraction)
    assert result.doc_type == DocType.RECETA
