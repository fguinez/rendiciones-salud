// Shared mutations: each refreshes the data and, when reversible, offers "Deshacer".

import { api, fmtMoney, plural } from "../core.js";
import { announce, confirmDialog, toast } from "../feedback.js";
import { missingFields, refresh, refreshOne, state, supportDocs } from "./store.js";

const failed = (err) => toast("No se pudo completar: " + err.message, "error");

const targetName = (rid) => `#${rid}`;

/** Move documents to ``target`` (null = one new rendición); returns the API result or null. */
export async function moveDocs(docIds, target, { undo = true, message } = {}) {
  const origin = new Map(docIds.map((id) => [id, state.docOwner.get(id)]));
  const groupings = snapshotGroupings([...origin.values(), target]);
  let res;
  try {
    res = await api("POST", "/api/documents/move", { document_ids: docIds, rendicion_id: target });
  } catch (err) {
    failed(err);
    return null;
  }
  await refresh();
  const text = message || (docIds.length === 1
    ? `Documento movido a ${target == null ? "la nueva rendición " : ""}${targetName(res.rendicion_id)}`
    : `${docIds.length} documentos movidos a ${target == null ? "la nueva rendición " : ""}${targetName(res.rendicion_id)}`);
  announce(text);
  toast(text, { kind: "ok", action: undo ? { label: "Deshacer", run: () => restoreOwners(origin, groupings) } : null });
  return res;
}

/** Documents and grouping of each rendición a move touches, so undo can put the grouping back. */
function snapshotGroupings(ids) {
  return [...new Set(ids)].map((id) => state.byId.get(id)).filter(Boolean).map((r) => ({
    docIds: r.documents.map((d) => d.id), confidence: r.grouping_confidence, reason: r.grouping_reason,
  }));
}

/** Give back its original grouping to each rendición whose documents are as they were before the move. */
async function restoreGroupings(groupings) {
  let changed = false;
  for (const g of groupings) {
    const r = state.byId.get(state.docOwner.get(g.docIds[0]));
    if (!r || r.documents.length !== g.docIds.length || !g.docIds.every((id) => state.docOwner.get(id) === r.id)) continue;
    if (r.grouping_confidence === g.confidence && r.grouping_reason === g.reason) continue;
    await api("PUT", `/api/rendiciones/${r.id}/grouping`, { confidence: g.confidence, reason: g.reason });
    changed = true;
  }
  if (changed) await refresh();
}

/** Put documents back where they were; owners that no longer exist become new rendiciones. */
async function restoreOwners(origin, groupings = []) {
  const groups = new Map();
  for (const [docId, rid] of origin) {
    if (rid == null) continue;
    if (!groups.has(rid)) groups.set(rid, []);
    groups.get(rid).push(docId);
  }
  for (const [rid, docIds] of groups) {
    const back = state.byId.has(rid) ? rid : null;
    await api("POST", "/api/documents/move", { document_ids: docIds, rendicion_id: back });
    await refresh(); // the next group may need to know which rendiciones survived
  }
  await restoreGroupings(groupings);
  announce("Movimiento deshecho");
  toast("Listo, se deshizo el cambio", "ok");
}

/** Merge rendiciones into the oldest one (moves every support document). */
export async function mergeInto(targetId, sourceIds) {
  const docIds = sourceIds.flatMap((id) => supportDocs(state.byId.get(id)).map((d) => d.id));
  if (!docIds.length) return null;
  const n = sourceIds.length + 1;
  return moveDocs(docIds, targetId, { message: `${n} rendiciones unidas en #${targetId}` });
}

const INVERSE = { lista: "borrador", borrador: "lista" };

/** Change the status of several rendiciones, offering undo; returns the ids that changed. */
export async function setStatus(ids, status, { message } = {}) {
  const before = new Map(ids.map((id) => [id, state.byId.get(id)?.status]));
  const done = [];
  const errors = [];
  for (const id of ids) {
    try {
      await api("POST", `/api/rendiciones/${id}/status`, { status });
      done.push(id);
    } catch (err) {
      errors.push(`#${id}: ${err.message}`);
    }
  }
  await refresh();
  if (errors.length) toast(`No se pudo cambiar ${errors.join(" · ")}`, "error");
  if (!done.length) return done;
  const reversible = done.filter((id) => INVERSE[status] === before.get(id));
  const text = message || (done.length === 1 ? `#${done[0]} ahora está «${statusWord(status)}»` : `${done.length} rendiciones ahora están «${statusWord(status)}»`);
  announce(text);
  toast(text, {
    kind: "ok",
    action: reversible.length ? {
      label: "Deshacer",
      run: async () => {
        for (const id of reversible) await api("POST", `/api/rendiciones/${id}/status`, { status: before.get(id) });
        await refresh();
        toast("Listo, se deshizo el cambio", "ok");
      },
    } : null,
  });
  return done;
}

const statusWord = (s) => ({ lista: "lista para enviar", borrador: "borrador" }[s] || s);

/** Borradores that can be marked ready, and why the others cannot. */
export function readiness(rs) {
  const ok = [];
  const skipped = [];
  for (const r of rs) {
    const missing = missingFields(r);
    if (r.status === "borrador" && !missing.length) ok.push(r.id);
    else if (r.status === "borrador") skipped.push(`#${r.id} (falta ${missing.join(", ")})`);
  }
  return { ok, skipped };
}

export async function deleteRendiciones(rs) {
  const docs = rs.reduce((acc, r) => acc + r.documents.length, 0);
  const total = rs.reduce((acc, r) => acc + (r.monto || 0), 0);
  const title = rs.length === 1 ? `¿Eliminar la rendición #${rs[0].id}?` : `¿Eliminar ${rs.length} rendiciones?`;
  const ok = await confirmDialog({
    title,
    body: `Se borran ${plural(docs, "documento", "documentos")} (${fmtMoney(total)}) de este computador. No se puede deshacer.`,
    confirmLabel: "Eliminar", danger: true,
  });
  if (!ok) return false;
  const errors = [];
  for (const r of rs) {
    try { await api("DELETE", `/api/rendiciones/${r.id}`); } catch (err) { errors.push(`#${r.id}: ${err.message}`); }
  }
  await refresh();
  if (errors.length) toast("No se pudo eliminar " + errors.join(" · "), "error");
  else toast(rs.length === 1 ? `Rendición #${rs[0].id} eliminada` : `${rs.length} rendiciones eliminadas`, "ok");
  return !errors.length;
}

export async function deleteDocument(doc) {
  const ok = await confirmDialog({
    title: "¿Eliminar este documento?",
    body: `«${doc.original_name}» se borra de este computador. No se puede deshacer.`,
    confirmLabel: "Eliminar", danger: true,
  });
  if (!ok) return false;
  try {
    await api("DELETE", `/api/documents/${doc.id}`);
  } catch (err) {
    failed(err);
    return false;
  }
  await refresh();
  toast("Documento eliminado", "ok");
  return true;
}

export async function patchRendicion(id, body) {
  await api("PATCH", `/api/rendiciones/${id}`, body);
  await refreshOne(id);
  toast(`Rendición #${id} guardada`, "ok");
}

export async function recompute(id) {
  const before = state.byId.get(id)?.status;
  try {
    await api("POST", `/api/rendiciones/${id}/recompute`);
  } catch (err) {
    failed(err);
    return;
  }
  await refreshOne(id);
  if (before === "lista" && state.byId.get(id)?.status === "borrador") {
    toast(`Valores recalculados. #${id} volvió a borrador porque le faltan datos`, "info");
  } else {
    toast("Valores recalculados desde los documentos", "ok");
  }
}

/** Throws on validation errors so the form can show them next to each field. */
export async function patchDocument(doc, body) {
  const rid = state.docOwner.get(doc.id);
  const before = state.byId.get(rid)?.status;
  await api("PATCH", `/api/documents/${doc.id}`, body);
  await refreshOne(rid);
  if (before === "lista" && state.byId.get(rid)?.status === "borrador") {
    toast(`Documento editado. #${rid} volvió a borrador porque le faltan datos`, "info");
  } else {
    toast("Documento editado", "ok");
  }
}

export { failed };
