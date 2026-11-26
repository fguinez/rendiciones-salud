// Uploads: drop target and pickers, per-file progress and retry, then grouping.

import { $, api, CSRF, docLabel, el, errorText, icon, plural } from "../core.js";
import { toast } from "../feedback.js";
import { openBatchReview } from "./batch.js";
import { refresh, setQuery } from "./store.js";

const ACCEPTED = /\.(pdf|jpe?g|png|heic|webp)$/i;
const CONCURRENCY = 3;

const readEntries = (reader) => new Promise((resolve, reject) => reader.readEntries(resolve, reject));

async function walkEntry(entry, prefix, out) {
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    out.push({ file, path: prefix + file.name });
  } else if (entry.isDirectory) {
    const reader = entry.createReader();
    let batch;
    do {
      batch = await readEntries(reader); // readEntries returns chunks; loop until empty
      for (const child of batch) await walkEntry(child, prefix + entry.name + "/", out);
    } while (batch.length > 0);
  }
}

async function filesFromDrop(dataTransfer) {
  const out = [];
  const entries = [];
  for (const item of dataTransfer.items || []) {
    const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
    if (entry) entries.push(entry);
    else if (item.kind === "file") out.push({ file: item.getAsFile(), path: item.getAsFile().name });
  }
  for (const entry of entries) await walkEntry(entry, "", out);
  return out;
}

const filesFromInput = (input) => Array.from(input.files).map((file) => ({ file, path: file.webkitRelativePath || file.name }));

export function chooseFiles(kind) {
  $(kind === "folder" ? "#folder-input" : "#file-input").click();
}

const ROW_TEXT = { queued: "En cola", uploading: "Subiendo…", reading: "Leyendo…" };

function makeRow(session, item, skipped) {
  const row = { item, state: skipped ? "skipped" : "queued", res: null, error: null };
  row.bar = el("progress", { max: "100", value: "0", "aria-label": `Progreso de ${item.path}` });
  row.status = el("span", { class: "up-status" });
  row.node = el("li", { class: "up-row" },
    el("span", { class: "up-icon" }, icon("file")),
    el("span", { class: "up-name", title: item.path }, item.path),
    row.status);
  row.update = () => paintRow(session, row);
  row.update();
  return row;
}

function paintRow(session, row) {
  row.node.className = `up-row ${row.state}`;
  const s = row.status;
  if (row.state === "queued" || row.state === "uploading") {
    s.replaceChildren(row.bar, el("span", { class: "sr-only" }, ROW_TEXT[row.state]));
  } else if (row.state === "reading") {
    s.replaceChildren(el("span", { class: "spinner", "aria-hidden": "true" }), "Leyendo…");
  } else if (row.state === "ok") {
    s.replaceChildren(icon("check", "ok"), docLabel(row.res.doc_type));
  } else if (row.state === "dup") {
    const rid = row.res.rendicion_id;
    s.replaceChildren(icon("file", "warn"), rid
      ? el("button", { class: "link-button", type: "button", onclick: () => setQuery({ r: rid }, { push: true }) }, `Ya estaba en #${rid}`)
      : "Ya estaba subido");
  } else if (row.state === "skipped") {
    s.replaceChildren(icon("x", "bad"), "Formato no compatible (usa PDF, JPG, PNG, HEIC o WEBP)");
  } else {
    s.replaceChildren(icon("alert", "bad"), el("span", { class: "up-error" }, row.error),
      el("button", { class: "button ghost small", type: "button", onclick: () => retry(session, row) }, icon("refresh"), "Reintentar"));
  }
}

function uploadOne(session, row) {
  row.state = "uploading";
  row.update();
  return new Promise((resolve) => {
    const form = new FormData();
    form.append("files", row.item.file, row.item.file.name);
    form.append("paths", row.item.path);
    form.append("batch_id", session.batchId);
    form.append("group", "false");
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/uploads");
    for (const [k, v] of Object.entries(CSRF)) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) row.bar.value = Math.round((e.loaded / e.total) * 100); };
    xhr.upload.onload = () => { row.state = "reading"; row.update(); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* ignore */ }
      const res = data.files && data.files[0];
      row.res = res || null;
      if (xhr.status >= 400 || !res) {
        row.state = "err";
        row.error = data.detail ? errorText(data.detail) : `El servidor respondió ${xhr.status}`;
      } else if (!res.ok) {
        row.state = "err";
        row.error = res.error || "No se pudo leer el archivo";
      } else {
        row.state = res.duplicate ? "dup" : "ok";
        if (!res.duplicate) session.docIds.add(res.document_id);
      }
      row.update();
      paintSession(session);
      resolve();
    };
    xhr.onerror = () => {
      row.state = "err";
      row.error = "Se cortó la conexión con la app";
      row.update();
      paintSession(session);
      resolve();
    };
    xhr.send(form);
  });
}

function newSession(items) {
  const accepted = items.filter((i) => ACCEPTED.test(i.file.name) && !i.file.name.startsWith("."));
  const skipped = items.filter((i) => !ACCEPTED.test(i.file.name) && !i.file.name.startsWith("."));
  const session = {
    batchId: (crypto.randomUUID ? crypto.randomUUID() : String(Date.now())).replace(/[^A-Za-z0-9_-]/g, ""),
    docIds: new Set(), grouping: { created: [], extended: [] }, phase: "uploading", expanded: false, rows: [],
  };
  session.rows = [...accepted.map((i) => makeRow(session, i, false)), ...skipped.map((i) => makeRow(session, i, true))];
  session.head = el("div", { class: "up-head" });
  session.list = el("ul", { class: "up-rows" }, session.rows.map((r) => r.node));
  session.node = el("section", { class: "upload-session", "aria-label": "Subida de documentos" }, session.head, session.list);
  $("#upload-tray").prepend(session.node);
  $("#upload-tray").hidden = false;
  return session;
}

function counts(session) {
  const by = (s) => session.rows.filter((r) => r.state === s).length;
  return { ok: by("ok"), dup: by("dup"), err: by("err"), skipped: by("skipped"), total: session.rows.length };
}

function paintSession(session) {
  const c = counts(session);
  const busy = session.phase !== "done";
  const done = session.rows.filter((r) => !["queued", "uploading", "reading"].includes(r.state)).length;
  const title = session.phase === "uploading" ? `Subiendo ${plural(c.total, "archivo", "archivos")} · ${done} de ${c.total}`
    : session.phase === "grouping" ? "Agrupando documentos…"
      : [c.ok && plural(c.ok, "subido", "subidos"), c.dup && plural(c.dup, "duplicado", "duplicados"),
        c.err && `${c.err} con error`, c.skipped && plural(c.skipped, "omitido", "omitidos")].filter(Boolean).join(" · ") || "Nada nuevo";
  const groups = session.grouping.created.length + session.grouping.extended.length;
  const detail = !busy && groups ? [
    session.grouping.created.length && plural(session.grouping.created.length, "rendición nueva", "rendiciones nuevas"),
    session.grouping.extended.length && plural(session.grouping.extended.length, "ampliada", "ampliadas"),
  ].filter(Boolean).join(", ") : "";
  const actions = [];
  if (!busy && session.docIds.size) {
    actions.push(el("button", { class: "button small", type: "button", onclick: () => review(session) }, "Revisar agrupación"));
  }
  if (!busy) {
    actions.push(el("button", {
      class: "button ghost small", type: "button", "aria-expanded": String(session.expanded),
      onclick: () => { session.expanded = !session.expanded; paintSession(session); },
    }, session.expanded ? "Ocultar detalle" : "Ver detalle"));
    actions.push(el("button", {
      class: "button ghost small icon-only", type: "button", "aria-label": "Cerrar resumen de subida",
      onclick: () => { session.node.remove(); if (!$("#upload-tray").children.length) $("#upload-tray").hidden = true; },
    }, icon("x")));
  }
  session.head.replaceChildren(
    el("span", { class: `up-head-icon${busy ? " busy" : c.err ? " bad" : " ok"}` }, busy ? el("span", { class: "spinner" }) : icon(c.err ? "alert" : "check")),
    el("div", { class: "up-head-text", role: "status" }, el("strong", {}, busy ? title : `Listo: ${title}`), detail ? el("span", { class: "muted" }, detail) : null),
    el("div", { class: "up-head-actions" }, actions));
  session.node.classList.toggle("collapsed", !busy && !session.expanded);
}

async function group(session) {
  session.phase = "grouping";
  paintSession(session);
  try {
    const g = await api("POST", `/api/batches/${session.batchId}/group`);
    session.grouping.created.push(...g.created);
    session.grouping.extended.push(...g.extended);
  } catch (err) {
    toast("Se subieron, pero no se pudieron agrupar: " + err.message, "error");
  }
  session.phase = "done";
  await refresh();
  paintSession(session);
}

function review(session) {
  openBatchReview({ docIds: [...session.docIds] });
}

async function uploadAll(items) {
  if (!items.length) return;
  const session = newSession(items);
  paintSession(session);
  const queue = session.rows.filter((r) => r.state === "queued");
  let next = 0;
  const worker = async () => { while (next < queue.length) await uploadOne(session, queue[next++]); };
  await Promise.all(Array.from({ length: CONCURRENCY }, worker));
  await group(session);
  const newDocs = session.docIds.size;
  if (newDocs >= 2) review(session);
  else if (newDocs === 1) {
    const r = session.grouping.created[0] || session.grouping.extended[0];
    if (r) toast(`Documento agregado a #${r}`, { kind: "ok", action: { label: "Abrir", run: () => setQuery({ r }, { push: true }) } });
  }
}

async function retry(session, row) {
  await uploadOne(session, row);
  if (row.state === "ok") {
    await group(session);
    toast("Archivo subido y agrupado", { kind: "ok", action: { label: "Revisar", run: () => review(session) } });
  }
}

function hasFiles(e) {
  return e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
}

export function initUpload() {
  for (const id of ["#file-input", "#folder-input"]) {
    const input = $(id);
    input.addEventListener("change", () => {
      const items = filesFromInput(input);
      input.value = "";
      uploadAll(items);
    });
  }
  for (const btn of document.querySelectorAll("[data-pick]")) btn.addEventListener("click", () => chooseFiles(btn.dataset.pick));

  // The whole Rendiciones page is a drop target for files from the computer.
  const overlay = $("#drop-overlay");
  let depth = 0;
  const canDrop = (e) => hasFiles(e) && !$("#view-rendiciones").hidden && !document.querySelector("dialog[open]");
  window.addEventListener("dragenter", (e) => {
    if (!canDrop(e)) return;
    depth += 1;
    overlay.hidden = false;
  });
  window.addEventListener("dragleave", (e) => {
    if (!hasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) overlay.hidden = true;
  });
  window.addEventListener("dragover", (e) => {
    e.preventDefault(); // otherwise the browser opens files dropped outside a target
    if (canDrop(e)) e.dataTransfer.dropEffect = "copy";
  });
  window.addEventListener("drop", async (e) => {
    e.preventDefault();
    depth = 0;
    overlay.hidden = true;
    if (!canDrop(e)) return;
    uploadAll(await filesFromDrop(e.dataTransfer));
  });
}
