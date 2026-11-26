"""Group documents that belong to the same medical attention into rendiciones."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from rendiciones.domain import PAYMENT_TYPES, SUPPORT_TYPES, DocType, TipoPrestacion

SUPPORT_MAX_DAYS = 3
FOLDER_MAX_DAYS = 45
SINGLE_DOC_CONFIDENCE = 0.6


@dataclass(frozen=True)
class GroupItem:
    """Grouping features of one document."""

    doc_id: int
    doc_type: DocType
    folder: str = ""
    folio: str | None = None
    provider_rut: str | None = None
    patient_rut: str | None = None
    fecha: date | None = None
    monto: int | None = None
    tipo_prestacion: TipoPrestacion | None = None
    rendicion_id: int | None = None  # already assigned (existing borrador)


@dataclass
class Group:
    """A proposed rendición."""

    doc_ids: list[int]
    confidence: float
    reasons: list[str] = field(default_factory=list)
    rendicion_id: int | None = None

    @property
    def reason(self) -> str:
        """Human-readable explanation (Spanish)."""
        return "; ".join(dict.fromkeys(self.reasons)) or "documento único"


@dataclass(frozen=True)
class _Link:
    a: int
    b: int
    confidence: float
    reason: str


class _UnionFind:
    def __init__(self, ids: list[int]) -> None:
        self.parent = {i: i for i in ids}

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _days(a: date | None, b: date | None) -> int | None:
    return abs((a - b).days) if a and b else None


def _patients_compatible(a: GroupItem, b: GroupItem) -> bool:
    return a.patient_rut is None or b.patient_rut is None or a.patient_rut == b.patient_rut


def _folder_conflict(a: GroupItem, b: GroupItem) -> bool:
    days = _days(a.fecha, b.fecha)
    if days is not None and days > FOLDER_MAX_DAYS:
        return True
    return not _patients_compatible(a, b)


def _links(items: list[GroupItem]) -> list[_Link]:
    links: list[_Link] = []
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            if not _patients_compatible(a, b):
                continue
            same_provider = bool(a.provider_rut and a.provider_rut == b.provider_rut)
            if a.folio and a.folio == b.folio:
                providers_ok = (
                    a.provider_rut is None
                    or b.provider_rut is None
                    or same_provider
                    or not (a.doc_type in PAYMENT_TYPES and b.doc_type in PAYMENT_TYPES)
                )
                if providers_ok:
                    links.append(_Link(a.doc_id, b.doc_id, 0.95, "mismo folio"))
                    continue
            if same_provider and a.fecha and a.fecha == b.fecha:
                links.append(_Link(a.doc_id, b.doc_id, 0.85, "mismo prestador y fecha"))
                continue
            if a.folder and a.folder == b.folder and not _folder_conflict(a, b):
                links.append(_Link(a.doc_id, b.doc_id, 0.75, "misma carpeta"))
    links.extend(_support_links(items))
    return links


def _support_links(items: list[GroupItem]) -> list[_Link]:
    """Attach each orden/receta/informe/programa to the nearest boleta by date."""
    links: list[_Link] = []
    payments = [p for p in items if p.doc_type in PAYMENT_TYPES and p.fecha]
    for support in items:
        if support.doc_type not in SUPPORT_TYPES or support.fecha is None:
            continue
        candidates: list[tuple[int, int, GroupItem]] = []
        for pay in payments:
            days = _days(support.fecha, pay.fecha)
            if days is None or days > SUPPORT_MAX_DAYS or not _patients_compatible(support, pay):
                continue
            provider_penalty = 0 if support.provider_rut == pay.provider_rut else 1
            candidates.append((provider_penalty, days, pay))
        if candidates:
            _, days, best = min(candidates, key=lambda c: (c[0], c[1], c[2].doc_id))
            confidence = 0.7 if days == 0 else 0.6
            links.append(_Link(support.doc_id, best.doc_id, confidence, "orden/receta con boleta"))
    return links


def _distinct_payments(a: GroupItem, b: GroupItem) -> bool:
    """True when two payment documents prove different services (folio, else date)."""
    if a.doc_type not in PAYMENT_TYPES or b.doc_type not in PAYMENT_TYPES:
        return False
    if a.folio and b.folio:
        return a.folio != b.folio
    if a.folio or b.folio:
        return False
    return bool(a.fecha and b.fecha and a.fecha != b.fecha)


def _payments_conflict(left: list[GroupItem], right: list[GroupItem]) -> bool:
    return any(_distinct_payments(a, b) for a in left for b in right)


def group_items(items: list[GroupItem]) -> list[Group]:
    """Cluster documents into groups using linking rules, strongest links first.

    Two components are not merged when that would mix different existing rendiciones,
    or two boletas of different services (different folios, or different dates when both
    folios are missing): each boleta is its own claim, even within the same folder.
    """
    by_id = {it.doc_id: it for it in items}
    uf = _UnionFind(list(by_id))
    members: dict[int, list[GroupItem]] = {it.doc_id: [it] for it in items}
    used: dict[int, list[_Link]] = {it.doc_id: [] for it in items}

    for link in sorted(_links(items), key=lambda lk: -lk.confidence):
        ra, rb = uf.find(link.a), uf.find(link.b)
        if ra == rb:
            used[ra].append(link)
            continue
        merged = members[ra] + members[rb]
        existing = {m.rendicion_id for m in merged if m.rendicion_id is not None}
        if len(existing) > 1:
            continue
        if _payments_conflict(members[ra], members[rb]):
            continue
        uf.union(ra, rb)
        root = uf.find(ra)
        other = rb if root == ra else ra
        members[root] = merged
        used[root] = used[ra] + used[rb] + [link]
        del members[other]
        del used[other]

    groups: list[Group] = []
    for root, group_members in sorted(members.items()):
        links = used[root]
        if len(group_members) == 1:
            confidence, reasons = SINGLE_DOC_CONFIDENCE, ["documento único"]
        else:
            confidence = min(lk.confidence for lk in links)
            reasons = [lk.reason for lk in links]
        existing_ids = [m.rendicion_id for m in group_members if m.rendicion_id is not None]
        groups.append(
            Group(
                doc_ids=sorted(m.doc_id for m in group_members),
                confidence=round(confidence, 2),
                reasons=reasons,
                rendicion_id=existing_ids[0] if existing_ids else None,
            )
        )
    return groups


@dataclass(frozen=True)
class Summary:
    """Aggregated fields for a rendición derived from its documents."""

    fecha_atencion: date | None
    monto: int | None
    tipo_prestacion: TipoPrestacion | None


def summarize(items: list[GroupItem]) -> Summary:
    """Derive fecha de atención, monto total and tipo de prestación from documents."""
    relevant = [
        i
        for i in items
        if i.doc_type not in {DocType.COMPROBANTE_BONIFICACION, DocType.DOCUMENTO_CHUBB}
    ]
    payments = [i for i in relevant if i.doc_type in PAYMENT_TYPES]
    dated = [i.fecha for i in (payments or relevant) if i.fecha]
    fecha = min(dated) if dated else None

    monto: int | None = None
    if payments:
        seen: dict[str, int] = {}
        loose = 0
        for p in payments:
            if p.monto is None:
                continue
            if p.folio:
                seen[f"{p.provider_rut}:{p.folio}"] = p.monto
            else:
                loose += p.monto
        total = sum(seen.values()) + loose
        monto = total or None
    else:
        amounts = [i.monto for i in relevant if i.monto]
        monto = max(amounts) if amounts else None

    votes: Counter[TipoPrestacion] = Counter()
    for item in relevant:
        if item.tipo_prestacion:
            votes[item.tipo_prestacion] += 2 if item.doc_type in PAYMENT_TYPES else 1
    tipo = votes.most_common(1)[0][0] if votes else None
    return Summary(fecha_atencion=fecha, monto=monto, tipo_prestacion=tipo)
