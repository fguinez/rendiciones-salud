"""Rule-based classifier for Chilean health documents (regex + keywords)."""

from __future__ import annotations

import re
import unicodedata
from datetime import date

from rendiciones import rut as rutlib
from rendiciones.domain import DocType, Extraction, TipoPrestacion
from rendiciones.recognition.base import RecognitionInput

MONTHS = {
    "ENERO": 1,
    "FEBRERO": 2,
    "MARZO": 3,
    "ABRIL": 4,
    "MAYO": 5,
    "JUNIO": 6,
    "JULIO": 7,
    "AGOSTO": 8,
    "SEPTIEMBRE": 9,
    "SETIEMBRE": 9,
    "OCTUBRE": 10,
    "NOVIEMBRE": 11,
    "DICIEMBRE": 12,
}

# (pattern, weight) per document type; patterns run on accent-free uppercase text.
DOC_TYPE_RULES: dict[DocType, list[tuple[str, float]]] = {
    DocType.COMPROBANTE_BONIFICACION: [
        (r"COMPROBANTE DE BONIFICACION", 6),
        (r"\bBONIFICACION\b", 2),
        (r"MONTO BONIFICADO", 3),
        (r"\bCOLMENA\b", 2),
        (r"\bISAPRE\b", 1.5),
        (r"\bCOPAGO\b", 1),
        (r"\bBONO\b", 1),
        (r"REEMBOLSO", 1),
    ],
    DocType.DOCUMENTO_CHUBB: [
        (r"\bCHUBB\b", 5),
        (r"SEGURO COMPLEMENTARIO", 3),
        (r"LIQUIDACION DE (SINIESTRO|REEMBOLSO)", 3),
        (r"\bSINIESTRO\b", 1.5),
        (r"\bPOLIZA\b", 1),
    ],
    DocType.BOLETA_HONORARIOS: [
        (r"BOLETA DE HONORARIOS", 7),
        (r"HONORARIOS", 2),
        (r"RETENCION", 1),
    ],
    DocType.FACTURA: [
        (r"FACTURA ELECTRONICA", 6),
        (r"\bFACTURA\b", 3),
        (r"\bIVA\b", 0.5),
    ],
    DocType.BOLETA_ELECTRONICA: [
        (r"BOLETA ELECTRONICA", 6),
        (r"\bBOLETA\b", 2),
        (r"TIMBRE ELECTRONICO", 1.5),
        (r"\bS\.?I\.?I\.?\b", 1),
        (r"VERIFIQUE (ESTE )?DOCUMENTO", 1),
    ],
    DocType.ORDEN_MEDICA: [
        (r"ORDEN MEDICA", 6),
        (r"ORDEN DE (EXAMEN|EXAMENES|ATENCION|PROCEDIMIENTO)", 5),
        (r"SOLICITUD DE EXAMEN", 4),
        (r"SE SOLICITA", 2),
        (r"\bINDICACION", 1),
        (r"DIAGNOSTICO", 1),
    ],
    DocType.RECETA: [
        (r"RECETA( MEDICA)?", 5),
        (r"\bRP\s*[:/.]", 2),
        (r"POSOLOGIA", 2),
        (r"CADA \d+ HORAS", 2),
        (r"\d+\s*MG\b", 1),
        (r"COMPRIMIDOS", 1),
    ],
    DocType.PROGRAMA_MEDICO: [
        (r"PROGRAMA MEDICO", 7),
        (r"PRESUPUESTO", 5),
        (r"COTIZACION", 3),
    ],
    DocType.INFORME_EXAMEN: [
        (r"INFORME", 3),
        (r"RESULTADO(S)? DE (EXAMEN|EXAMENES|LABORATORIO)", 5),
        (r"VALOR(ES)? DE REFERENCIA", 3),
        (r"\bRESULTADO\b", 1.5),
        (r"HEMOGRAMA|PERFIL (LIPIDICO|BIOQUIMICO)|IMPRESION DIAGNOSTICA", 1.5),
    ],
}

PRESTACION_RULES: list[tuple[TipoPrestacion, str]] = [
    (TipoPrestacion.HOSPITALIZACION, r"HOSPITALIZACION|DIA CAMA|PABELLON|EGRESO HOSPITAL"),
    (TipoPrestacion.LENTES, r"\bLENTES\b|OPTICA|CRISTALES|MARCO OPTICO|LENTE DE CONTACTO"),
    (TipoPrestacion.DENTAL, r"DENTAL|ODONTOLOG|DENTISTA|ENDODONCIA|ORTODONCIA|PROFILAXIS"),
    (
        TipoPrestacion.OTROS_PROFESIONALES,
        r"KINESIOLOG|KINESITERAPIA|PSICOLOG|PSICOTERAPIA|FONOAUDIOLOG|NUTRICIONIST|"
        r"TERAPIA OCUPACIONAL|MATRONA",
    ),
    (
        TipoPrestacion.IMAGENOLOGIA,
        r"RADIOGRAFIA|ECOGRAFIA|ECOTOMOGRAFIA|RESONANCIA|SCANNER|TOMOGRAFIA|\bTAC\b|"
        r"MAMOGRAFIA|IMAGENOLOGIA|DENSITOMETRIA",
    ),
    (
        TipoPrestacion.EXAMENES_LABORATORIO,
        r"LABORATORIO|HEMOGRAMA|GLICEMIA|PERFIL LIPIDICO|PERFIL BIOQUIMICO|ORINA|"
        r"\bTSH\b|CREATININA|COLESTEROL|EXAMEN(ES)? DE SANGRE",
    ),
    (
        TipoPrestacion.PROCEDIMIENTO,
        r"PROCEDIMIENTO|ENDOSCOPIA|COLONOSCOPIA|BIOPSIA|CIRUGIA MENOR|INFILTRACION",
    ),
    (TipoPrestacion.MEDICAMENTOS, r"FARMACIA|MEDICAMENTO|COMPRIMIDOS|\bRECETA\b|JARABE"),
    (TipoPrestacion.CONSULTA_MEDICA, r"CONSULTA|ATENCION MEDICA|CONTROL MEDICO|TELEMEDICINA"),
]

RUT_RE = re.compile(r"(?<![\d.])(\d{1,2}\.?\d{3}\.?\d{3}\s?-\s?[\dK])(?![\dA-Z])")
DATE_NUM_RE = re.compile(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})(?!\d)")
DATE_ISO_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
DATE_TEXT_RE = re.compile(r"(\d{1,2})\s+DE\s+([A-Z]+)\s+(?:DE|DEL)\s+(\d{4})")
AMOUNT_RE = re.compile(r"\$\s*:?\s*(\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{1,2})?")
BARE_AMOUNT_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+)(?![\d.,]|\s?-)")
FOLIO_RE = re.compile(
    r"(?:FOLIO|BOLETA|FACTURA)?\s*(?:N\s?[°ºO]|NRO\.?|NUMERO|FOLIO)\s*[:.]?\s*(\d{1,12})\b"
)
PATIENT_HINT = re.compile(
    r"PACIENTE|BENEFICIARIO|AFILIADO|RECEPTOR|SENOR\(?ES\)?|CLIENTE|TITULAR|ASEGURADO"
)
PROVIDER_LABEL_RE = re.compile(
    r"^\s*(?:RAZON SOCIAL|EMISOR|PRESTADOR|PROFESIONAL|INSTITUCION)\s*:\s*(.+)"
)
DATE_LABELS = re.compile(r"FECHA\s+(?:DE\s+)?(?:ATENCION|PRESTACION|EMISION)|FECHA\s*:")
TOTAL_LABELS = re.compile(r"TOTAL|MONTO|VALOR A PAGAR|A PAGAR")
HONORARIOS_TOTAL_LABEL = re.compile(r"TOTAL\s+(?:DE\s+)?HONORARIOS?")
NET_LABELS = re.compile(r"RETEN|IMPTO|IMPUESTO|\bLIQUIDO\b")
DASHES = str.maketrans("\u2010\u2011\u2012\u2013\u2014\u2212", "------")
BONIF_LABELS = re.compile(r"BONIFICAD|BONIFICACION")
GLOSA_LABEL_RE = re.compile(r"POR\s+ATENCION\s+PROFESIONAL\s*:?")
GLOSA_LABEL_ORIGINAL_RE = re.compile(
    r"por\s+atenci(?:o\u0301?|\u00f3)n\s+profesional\s*:?", re.IGNORECASE
)
TRAILING_AMOUNT_RE = re.compile(r"[\s$:]*\d{1,3}(?:\.\d{3})+\s*$")


def normalize_text(text: str) -> str:
    """Uppercase and strip accents so rules can use plain ASCII patterns."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.upper().replace("\r", "").translate(DASHES)


def classify_doc_type(norm: str) -> tuple[DocType, float]:
    """Return the best-scoring document type and a 0..1 confidence."""
    scores: dict[DocType, float] = {}
    for doc_type, rules in DOC_TYPE_RULES.items():
        score = sum(weight for pattern, weight in rules if re.search(pattern, norm))
        if score:
            scores[doc_type] = score
    if not scores:
        return DocType.OTRO, 0.2
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = (best_score - runner_up) / best_score
    confidence = min(0.95, 0.4 + min(best_score, 8) / 16 + margin * 0.15)
    return best, round(confidence, 2)


def parse_amount(raw: str) -> int | None:
    """Parse ``$ 45.000`` style CLP amounts into an integer."""
    digits = raw.replace(".", "")
    return int(digits) if digits.isdigit() else None


def _safe_date(year: int, month: int, day: int) -> date | None:
    if year < 100:
        year += 2000
    try:
        found = date(year, month, day)
    except ValueError:
        return None
    return found if 2000 <= found.year <= 2100 else None


def find_dates(line: str) -> list[date]:
    """All valid dates in a line (dd/mm/yyyy, yyyy-mm-dd, '5 de octubre de 2026')."""
    found: list[date] = []
    for d, m, y in DATE_NUM_RE.findall(line):
        parsed = _safe_date(int(y), int(m), int(d))
        if parsed:
            found.append(parsed)
    for y, m, d in DATE_ISO_RE.findall(line):
        parsed = _safe_date(int(y), int(m), int(d))
        if parsed:
            found.append(parsed)
    for d, month_name, y in DATE_TEXT_RE.findall(line):
        month = MONTHS.get(month_name)
        if month:
            parsed = _safe_date(int(y), month, int(d))
            if parsed:
                found.append(parsed)
    return found


def extract_date(lines: list[str]) -> date | None:
    """Prefer dates on labelled lines (fecha de atención/emisión), else the first date."""
    for i, line in enumerate(lines):
        if DATE_LABELS.search(line):
            window = [line, lines[i + 1]] if i + 1 < len(lines) else [line]
            for candidate in window:
                dates = find_dates(candidate)
                if dates:
                    return dates[0]
    for line in lines:
        dates = find_dates(line)
        if dates:
            return dates[0]
    return None


def _line_amounts(line: str, labelled: bool) -> list[int]:
    raw = AMOUNT_RE.findall(line)
    if not raw and labelled:
        raw = BARE_AMOUNT_RE.findall(line)
    return [a for a in (parse_amount(m) for m in raw) if a]


def extract_amount(lines: list[str], doc_type: DocType) -> tuple[int | None, int | None]:
    """Return ``(total, monto_bonificado)`` in CLP.

    A boleta de honorarios is claimed for its gross "Total Honorarios": retention and net
    (líquido) amounts are ignored. A label whose amount sits on the next line is supported.
    """
    is_boleta = doc_type == DocType.BOLETA_HONORARIOS
    honorarios: list[int] = []
    totals: list[int] = []
    bonif: list[int] = []
    every: list[int] = []
    for i, line in enumerate(lines):
        net = NET_LABELS.search(line) if is_boleta else None
        if net:
            line = line[: net.start()]
        is_honorarios = is_boleta and bool(HONORARIOS_TOTAL_LABEL.search(line))
        is_bonif = bool(BONIF_LABELS.search(line))
        is_total = bool(TOTAL_LABELS.search(line)) or (not is_boleta and "LIQUIDO" in line)
        labelled = is_honorarios or is_bonif or is_total
        amounts = _line_amounts(line, labelled)
        if not amounts and labelled and not net and not (is_bonif and is_total):
            following = lines[i + 1] if i + 1 < len(lines) else ""
            if not (
                NET_LABELS.search(following)
                or TOTAL_LABELS.search(following)
                or BONIF_LABELS.search(following)
            ):
                amounts = _line_amounts(following, labelled=True)
        if not amounts:
            continue
        every.extend(amounts)
        if is_bonif:
            bonif.append(amounts[-1])
        elif is_honorarios:
            honorarios.append(amounts[-1])
        elif labelled:
            totals.append(amounts[-1])
    if honorarios:
        total: int | None = max(honorarios)
    else:
        total = max(totals) if totals else (max(every) if every else None)
    bonificado = max(bonif) if bonif else None
    if doc_type != DocType.COMPROBANTE_BONIFICACION and bonificado and not total:
        total = bonificado
    return total, bonificado


def extract_glosa(original_lines: list[str], norm_lines: list[str]) -> str | None:
    """Service description of a boleta de honorarios ("Por atención profesional: …")."""
    for i, norm in enumerate(norm_lines):
        norm_match = GLOSA_LABEL_RE.search(norm)
        if not norm_match:
            continue
        match = GLOSA_LABEL_ORIGINAL_RE.search(original_lines[i]) or norm_match
        glosa = original_lines[i][match.end() :]
        following = norm_lines[i + 1] if i + 1 < len(norm_lines) else ""
        if not glosa.strip() and following and not _is_amount_label(following):
            glosa = original_lines[i + 1]
        glosa = TRAILING_AMOUNT_RE.sub("", glosa).strip(" :-")
        return glosa[:200] or None
    return None


def _is_amount_label(norm: str) -> bool:
    return any(r.search(norm) for r in (TOTAL_LABELS, HONORARIOS_TOTAL_LABEL, NET_LABELS))


def extract_folio(norm: str) -> str | None:
    """Return the document number (folio / N° boleta)."""
    for match in FOLIO_RE.finditer(norm):
        folio = match.group(1).lstrip("0")
        if folio:
            return folio
    return None


def extract_ruts(lines: list[str]) -> tuple[str | None, str | None, int | None]:
    """Return ``(provider_rut, patient_rut, provider_line_index)``."""
    provider: str | None = None
    provider_idx: int | None = None
    patient: str | None = None
    for i, line in enumerate(lines):
        for match in RUT_RE.finditer(line):
            canonical = rutlib.normalize(match.group(1))
            if canonical is None:
                continue
            context = line[: match.start()]
            if i > 0 and not context.strip():
                context = lines[i - 1]
            if PATIENT_HINT.search(context):
                patient = patient or canonical
            elif provider is None:
                provider, provider_idx = canonical, i
            elif patient is None and canonical != provider:
                patient = canonical
    return provider, patient, provider_idx


def extract_provider_name(
    original_lines: list[str], norm_lines: list[str], provider_idx: int | None
) -> str | None:
    """Find the issuer's name from a label, the RUT line or the document header."""
    for orig, norm in zip(original_lines, norm_lines, strict=True):
        match = PROVIDER_LABEL_RE.search(norm)
        if match:
            start = match.start(1)
            name = orig[start:].strip(" :-")
            name = re.split(r"\s{2,}|\bRUT\b|R\.U\.T", name, flags=re.IGNORECASE)[0].strip()
            if len(name) >= 3:
                return name[:120]
    if provider_idx is not None:
        orig = original_lines[provider_idx]
        before = re.split(r"R\.?U\.?T\.?", orig, flags=re.IGNORECASE)[0].strip(" :-|")
        if len(before) >= 3 and not RUT_RE.search(normalize_text(before)):
            return before[:120]
        if provider_idx > 0:
            prev = original_lines[provider_idx - 1].strip()
            if len(prev) >= 3 and not re.search(r"\d{4,}", prev):
                return prev[:120]
    for orig in original_lines[:3]:
        candidate = orig.strip()
        if len(candidate) >= 3 and not re.search(r"\d{3,}", candidate):
            return candidate[:120]
    return None


def suggest_prestacion(norm: str, doc_type: DocType) -> TipoPrestacion | None:
    """Suggest the 'tipo de prestación' from keywords."""
    for tipo, pattern in PRESTACION_RULES:
        if re.search(pattern, norm):
            return tipo
    if doc_type == DocType.RECETA:
        return TipoPrestacion.MEDICAMENTOS
    if doc_type == DocType.BOLETA_HONORARIOS:
        return TipoPrestacion.CONSULTA_MEDICA
    return None


class RuleBasedClassifier:
    """Default offline classifier."""

    name = "rules"

    def classify(self, doc: RecognitionInput) -> Extraction:
        """Classify ``doc`` with regex/keyword rules."""
        return extract_from_text(doc.text)


def extract_from_text(text: str) -> Extraction:
    """Apply all rules to raw text."""
    original_lines = [line for line in text.replace("\r", "").split("\n") if line.strip()]
    norm_lines = [normalize_text(line) for line in original_lines]
    norm = "\n".join(norm_lines)
    if not norm.strip():
        return Extraction(doc_type=DocType.OTRO, confidence=0.0, classifier="rules")
    doc_type, confidence = classify_doc_type(norm)
    provider_rut, patient_rut, provider_idx = extract_ruts(norm_lines)
    total, bonificado = extract_amount(norm_lines, doc_type)
    extras: dict[str, object] = {}
    if bonificado is not None:
        extras["monto_bonificado"] = bonificado
    if doc_type == DocType.BOLETA_HONORARIOS:
        glosa = extract_glosa(original_lines, norm_lines)
        if glosa:
            extras["descripcion"] = glosa
    return Extraction(
        doc_type=doc_type,
        provider_name=extract_provider_name(original_lines, norm_lines, provider_idx),
        provider_rut=provider_rut,
        patient_rut=patient_rut,
        fecha=extract_date(norm_lines),
        monto=total,
        folio=extract_folio(norm),
        tipo_prestacion=suggest_prestacion(norm, doc_type),
        confidence=confidence,
        classifier="rules",
        extras=extras,
    )
