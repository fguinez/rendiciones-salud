"""Optional LLM classifier using the Anthropic API (document/image blocks + JSON schema)."""

from __future__ import annotations

import base64
import io
import json
import logging
from datetime import date
from typing import Any

from rendiciones import rut as rutlib
from rendiciones.domain import DocType, Extraction, TipoPrestacion
from rendiciones.recognition.base import Classifier, RecognitionInput
from rendiciones.recognition.text import load_image

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-5-5"
MAX_FILE_BYTES = 20 * 1024 * 1024
API_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}

SYSTEM_PROMPT = (
    "Eres un asistente que clasifica documentos de salud chilenos para rendir reembolsos "
    "a una Isapre y a un seguro complementario. Extrae solo lo que aparece en el documento; "
    'usa null cuando un dato no esté. Montos en pesos chilenos como enteros ("$ 45.000" → '
    "45000). En una boleta de honorarios monto_total es el Total Honorarios (bruto), nunca "
    "la retención ni el líquido. Fechas en formato YYYY-MM-DD. RUT con guion y dígito verificador "
    '("12.345.678-5" → "12345678-5"). provider es quien emite el documento o presta la '
    "atención; patient es la persona atendida. descripcion es la glosa del servicio (en "
    'boletas de honorarios, el texto de "Por atención profesional").'
)

_NULLABLE_STR = {"type": ["string", "null"]}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": [d.value for d in DocType]},
        "provider_name": _NULLABLE_STR,
        "provider_rut": _NULLABLE_STR,
        "patient_rut": _NULLABLE_STR,
        "fecha": _NULLABLE_STR,
        "monto_total": {"type": ["integer", "null"]},
        "monto_bonificado": {"type": ["integer", "null"]},
        "folio": _NULLABLE_STR,
        "descripcion": _NULLABLE_STR,
        "tipo_prestacion": {
            "type": ["string", "null"],
            "enum": [*[t.value for t in TipoPrestacion], None],
        },
        "confidence": {"type": "number"},
    },
    "required": [
        "doc_type",
        "provider_name",
        "provider_rut",
        "patient_rut",
        "fecha",
        "monto_total",
        "monto_bonificado",
        "folio",
        "descripcion",
        "tipo_prestacion",
        "confidence",
    ],
    "additionalProperties": False,
}


def _file_block(doc: RecognitionInput) -> dict[str, Any] | None:
    data = doc.path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        return None
    if doc.mime == "application/pdf":
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.b64encode(data).decode(),
            },
        }
    media_type = doc.mime
    if media_type not in API_IMAGE_TYPES:
        buf = io.BytesIO()
        load_image(doc.path).save(buf, format="JPEG", quality=90)
        data, media_type = buf.getvalue(), "image/jpeg"
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(data).decode(),
        },
    }


def parse_llm_payload(payload: dict[str, Any]) -> Extraction:
    """Convert the model's JSON into an Extraction, validating RUTs and dates."""

    def _rut(value: Any) -> str | None:
        return rutlib.normalize(value) if isinstance(value, str) else None

    fecha: date | None = None
    if isinstance(payload.get("fecha"), str):
        try:
            fecha = date.fromisoformat(payload["fecha"])
        except ValueError:
            fecha = None
    try:
        doc_type = DocType(payload.get("doc_type", "otro"))
    except ValueError:
        doc_type = DocType.OTRO
    tipo_raw = payload.get("tipo_prestacion")
    try:
        tipo = TipoPrestacion(tipo_raw) if tipo_raw else None
    except ValueError:
        tipo = None
    extras: dict[str, Any] = {}
    if isinstance(payload.get("monto_bonificado"), int):
        extras["monto_bonificado"] = payload["monto_bonificado"]
    descripcion = payload.get("descripcion")
    if isinstance(descripcion, str) and descripcion.strip():
        extras["descripcion"] = descripcion.strip()[:200]
    monto = payload.get("monto_total")
    folio = payload.get("folio")
    name = payload.get("provider_name")
    conf = payload.get("confidence")
    return Extraction(
        doc_type=doc_type,
        provider_name=name[:120] if isinstance(name, str) else None,
        provider_rut=_rut(payload.get("provider_rut")),
        patient_rut=_rut(payload.get("patient_rut")),
        fecha=fecha,
        monto=monto if isinstance(monto, int) else None,
        folio=str(folio).lstrip("0") or None if folio else None,
        tipo_prestacion=tipo,
        confidence=max(0.0, min(1.0, float(conf))) if isinstance(conf, int | float) else 0.7,
        classifier="llm",
        extras=extras,
    )


class AnthropicClassifier:
    """Classifies by sending the file to Claude; falls back to another classifier on error."""

    name = "llm"

    def __init__(self, api_key: str, fallback: Classifier, client: Any | None = None) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=120)
        self._client: Any = client
        self._fallback = fallback

    def classify(self, doc: RecognitionInput) -> Extraction:
        """Classify with the LLM; on any failure use the fallback classifier."""
        try:
            block = _file_block(doc)
            if block is None:
                return self._fallback.classify(doc)
            response = self._client.messages.create(
                model=MODEL,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
                },
                messages=[
                    {
                        "role": "user",
                        "content": [
                            block,
                            {
                                "type": "text",
                                "text": "Clasifica este documento y extrae sus datos.",
                            },
                        ],
                    }
                ],
            )
            if getattr(response, "stop_reason", None) == "refusal":
                logger.warning("llm classifier refused; using fallback")
                return self._fallback.classify(doc)
            text = next(b.text for b in response.content if getattr(b, "type", "") == "text")
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise TypeError("unexpected payload")
            return parse_llm_payload(payload)
        except Exception as exc:
            logger.warning("llm classifier failed (%s); using fallback", type(exc).__name__)
            return self._fallback.classify(doc)
