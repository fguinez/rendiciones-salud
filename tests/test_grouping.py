"""Tests for grouping documents into rendiciones."""

from __future__ import annotations

from datetime import date

import pytest

from rendiciones.domain import DocType, TipoPrestacion
from rendiciones.grouping import SINGLE_DOC_CONFIDENCE, GroupItem, group_items, summarize

PATIENT = "11111111-1"
OTHER_PATIENT = "15555555-6"
LAB = "76123456-0"
CLINIC = "77654321-7"
THERAPIST = "22333444-K"


def boleta(doc_id: int, **kwargs: object) -> GroupItem:
    """A boleta electrónica item with sensible synthetic defaults."""
    defaults: dict[str, object] = {
        "doc_type": DocType.BOLETA_ELECTRONICA,
        "provider_rut": LAB,
        "patient_rut": PATIENT,
        "fecha": date(2026, 10, 3),
    }
    defaults.update(kwargs)
    return GroupItem(doc_id=doc_id, **defaults)  # type: ignore[arg-type]


def test_same_folio_groups_together() -> None:
    """A boleta and the comprobante that cites its folio form one group."""
    items = [
        boleta(1, folio="981"),
        GroupItem(
            doc_id=2,
            doc_type=DocType.COMPROBANTE_BONIFICACION,
            folio="981",
            provider_rut=LAB,
            fecha=date(2026, 10, 10),
        ),
    ]

    groups = group_items(items)

    assert len(groups) == 1
    assert groups[0].doc_ids == [1, 2]
    assert groups[0].confidence == 0.95
    assert groups[0].reason == "mismo folio"


def test_same_provider_and_date_groups_together() -> None:
    """Two documents from the same provider on the same date are grouped."""
    items = [
        boleta(1, folio="10"),
        GroupItem(
            doc_id=2, doc_type=DocType.INFORME_EXAMEN, provider_rut=LAB, fecha=date(2026, 10, 3)
        ),
    ]

    groups = group_items(items)

    assert [g.doc_ids for g in groups] == [[1, 2]]
    assert "mismo prestador y fecha" in groups[0].reason


def test_two_boletas_with_different_folios_stay_apart() -> None:
    """Same provider and date but different payment folios are separate rendiciones."""
    items = [boleta(1, folio="10"), boleta(2, folio="11")]

    groups = group_items(items)

    assert [g.doc_ids for g in groups] == [[1], [2]]
    assert all(g.confidence == SINGLE_DOC_CONFIDENCE for g in groups)


def test_folder_hint_groups_unrelated_documents() -> None:
    """Documents uploaded in the same sub-folder are grouped with lower confidence."""
    items = [
        boleta(1, folder="consulta-octubre", folio="10"),
        GroupItem(
            doc_id=2,
            doc_type=DocType.OTRO,
            folder="consulta-octubre",
            fecha=date(2026, 10, 20),
        ),
    ]

    groups = group_items(items)

    assert [g.doc_ids for g in groups] == [[1, 2]]
    assert groups[0].reason == "misma carpeta"
    assert groups[0].confidence == 0.75


@pytest.mark.parametrize(
    "other",
    [
        GroupItem(doc_id=2, doc_type=DocType.OTRO, folder="f", patient_rut=OTHER_PATIENT),
        GroupItem(doc_id=2, doc_type=DocType.OTRO, folder="f", fecha=date(2027, 3, 1)),
    ],
    ids=["different-patient", "too-far-apart"],
)
def test_folder_hint_respects_conflicts(other: GroupItem) -> None:
    """The folder hint does not join different patients or distant dates."""
    items = [boleta(1, folder="f", folio="10"), other]

    groups = group_items(items)

    assert len(groups) == 2


def honorarios(doc_id: int, day: int, **kwargs: object) -> GroupItem:
    """A weekly boleta de honorarios from the same therapist, uploaded in one folder."""
    defaults: dict[str, object] = {
        "doc_type": DocType.BOLETA_HONORARIOS,
        "folder": "terapia",
        "provider_rut": THERAPIST,
        "fecha": date(2026, 1, day),
        "monto": 40000,
        "tipo_prestacion": TipoPrestacion.OTROS_PROFESIONALES,
    }
    defaults.update(kwargs)
    return GroupItem(doc_id=doc_id, **defaults)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "items",
    [
        [honorarios(i, day, folio=str(100 + i)) for i, day in enumerate([5, 12, 19, 26], 1)],
        [honorarios(i, day) for i, day in enumerate([5, 12, 19, 26], 1)],
        [
            honorarios(i, day, folio=str(100 + i), provider_rut=None, patient_rut=None)
            for i, day in enumerate([5, 12, 19, 26], 1)
        ],
    ],
    ids=["different-folios", "missing-folios", "missing-ruts"],
)
def test_weekly_boletas_in_same_folder_stay_apart(items: list[GroupItem]) -> None:
    """Each boleta de honorarios is its own rendición, even within one uploaded folder."""
    groups = group_items(items)

    assert [g.doc_ids for g in groups] == [[1], [2], [3], [4]]


@pytest.mark.parametrize(
    ("second", "expected"),
    [
        (honorarios(2, 5, folio="900"), [[1, 2]]),
        (honorarios(2, 12, folio="900"), [[1, 2]]),
        (honorarios(2, 12), [[1], [2]]),
    ],
    ids=["one-folio-same-date", "one-folio-other-date", "no-folios-other-dates"],
)
def test_boleta_dates_compared_only_without_folios(
    second: GroupItem, expected: list[list[int]]
) -> None:
    """A boleta photo without folio stays with its SII PDF; dates split only folio-less ones."""
    items = [honorarios(1, 5), second]

    groups = group_items(items)

    assert [g.doc_ids for g in groups] == expected


def test_folder_document_does_not_bridge_two_boletas() -> None:
    """A non-payment document in the folder joins one boleta without merging the others."""
    items = [
        honorarios(1, 5, folio="101"),
        honorarios(2, 12, folio="102"),
        GroupItem(doc_id=3, doc_type=DocType.OTRO, folder="terapia", fecha=date(2026, 1, 8)),
    ]

    groups = group_items(items)

    assert len(groups) == 2
    assert sorted(len(g.doc_ids) for g in groups) == [1, 2]


def test_weekly_boletas_keep_their_own_orden_medica() -> None:
    """An orden médica still joins its boleta while the other weekly boletas stay apart."""
    items = [
        honorarios(1, 5, folio="101"),
        honorarios(2, 12, folio="102"),
        GroupItem(
            doc_id=3,
            doc_type=DocType.ORDEN_MEDICA,
            folder="terapia",
            provider_rut=THERAPIST,
            fecha=date(2026, 1, 5),
        ),
        honorarios(4, 12, folio="102", folder="otra-carpeta"),
    ]

    groups = group_items(items)

    assert [g.doc_ids for g in groups] == [[1, 3], [2, 4]]


def test_support_document_joins_nearest_boleta() -> None:
    """An orden médica within a few days is attached to the closest boleta."""
    items = [
        boleta(1, folio="10", fecha=date(2026, 10, 3)),
        boleta(2, folio="11", fecha=date(2026, 10, 20), provider_rut=CLINIC),
        GroupItem(
            doc_id=3,
            doc_type=DocType.ORDEN_MEDICA,
            provider_rut=CLINIC,
            patient_rut=PATIENT,
            fecha=date(2026, 10, 1),
        ),
    ]

    groups = group_items(items)

    assert [g.doc_ids for g in groups] == [[1, 3], [2]]
    assert groups[0].reason == "orden/receta con boleta"


def test_documents_of_different_patients_never_group() -> None:
    """Even with the same folio, different patients are kept apart."""
    items = [boleta(1, folio="981"), boleta(2, folio="981", patient_rut=OTHER_PATIENT)]

    groups = group_items(items)

    assert len(groups) == 2


def test_existing_rendiciones_are_not_merged() -> None:
    """A new document can join one existing rendición but never merge two of them."""
    items = [
        boleta(1, folio="981", rendicion_id=10),
        GroupItem(doc_id=2, doc_type=DocType.OTRO, folio="981", rendicion_id=20),
        GroupItem(doc_id=3, doc_type=DocType.COMPROBANTE_BONIFICACION, folio="981"),
    ]

    groups = group_items(items)

    assert len(groups) == 2
    joined = next(g for g in groups if 3 in g.doc_ids)
    assert joined.rendicion_id in {10, 20}


def test_summarize_prefers_payments() -> None:
    """Summary uses boleta dates/amounts (deduplicated by folio) and weighted tipo votes."""
    items = [
        boleta(1, folio="10", monto=24000, tipo_prestacion=TipoPrestacion.EXAMENES_LABORATORIO),
        boleta(2, folio="10", monto=24000),
        boleta(3, folio="11", monto=6000, fecha=date(2026, 10, 2)),
        GroupItem(
            doc_id=4,
            doc_type=DocType.ORDEN_MEDICA,
            fecha=date(2026, 9, 1),
            tipo_prestacion=TipoPrestacion.CONSULTA_MEDICA,
        ),
    ]

    summary = summarize(items)

    assert summary.monto == 30000
    assert summary.fecha_atencion == date(2026, 10, 2)
    assert summary.tipo_prestacion == TipoPrestacion.EXAMENES_LABORATORIO


def test_summarize_ignores_portal_documents() -> None:
    """Comprobantes and Chubb documents do not affect the rendición summary."""
    items = [
        GroupItem(doc_id=1, doc_type=DocType.COMPROBANTE_BONIFICACION, monto=99999),
        GroupItem(doc_id=2, doc_type=DocType.PROGRAMA_MEDICO, monto=5000),
    ]

    summary = summarize(items)

    assert summary.monto == 5000
