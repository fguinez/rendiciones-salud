// "Flujo" view: drag accounts into stages to define where each rendición goes.

import { $, api, el, shared } from "./core.js";
import { toast } from "./feedback.js";
import { loadWorkflow, renderRoute } from "./pipeline.js";

export const editor = { stages: [], saved: [], accounts: {}, problems: [], checking: false, checkTimer: null, dragKey: null };
const sameStages = (a, b) => JSON.stringify(a) === JSON.stringify(b);
export const isDirty = () => !sameStages(editor.stages, editor.saved);

export async function loadEditor() {
  await loadWorkflow();
  editor.stages = shared.workflow.stages.map((s) => [...s]);
  editor.saved = shared.workflow.stages.map((s) => [...s]);
  editor.problems = [];
  try {
    const accounts = await api("GET", "/api/accounts");
    editor.accounts = Object.fromEntries(accounts.map((a) => [a.portal, a]));
  } catch { editor.accounts = {}; }
  renderEditor();
}

export function setStages(stages) {
  editor.stages = stages.filter((s) => s.length);
  editor.problems = [];
  editor.checking = true;
  renderEditor();
  clearTimeout(editor.checkTimer);
  editor.checkTimer = setTimeout(checkEditor, 150);
}

async function checkEditor() {
  try {
    const { problems } = await api("POST", "/api/workflow/check", { stages: editor.stages });
    editor.problems = problems;
  } catch (err) {
    editor.problems = ["No se pudo validar: " + err.message];
  }
  editor.checking = false;
  renderEditor();
}

export function without(key) {
  return editor.stages.map((s) => s.filter((k) => k !== key));
}

function moveTo(key, stageIndex) {
  const stages = without(key);
  stages[stageIndex] = [...(stages[stageIndex] || []), key];
  setStages(stages);
}

function insertStage(key, gapIndex) {
  const stages = editor.stages.map((s) => s.filter((k) => k !== key));
  stages.splice(gapIndex, 0, [key]);
  setStages(stages);
}

function shift(key, delta) {
  const from = editor.stages.findIndex((s) => s.includes(key));
  const alone = editor.stages[from].length === 1;
  const to = from + delta;
  if (to < 0) insertStage(key, 0);
  else if (to >= editor.stages.length) insertStage(key, editor.stages.length);
  else if (alone) {
    const stages = editor.stages.map((s) => [...s]);
    stages[to].push(key);
    stages.splice(from, 1);
    setStages(stages);
  } else moveTo(key, to);
}

export function dropTarget(node, onDrop) {
  node.addEventListener("dragover", (e) => {
    if (!editor.dragKey) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    node.classList.add("over");
  });
  node.addEventListener("dragleave", (e) => {
    if (!node.contains(e.relatedTarget)) node.classList.remove("over");
  });
  node.addEventListener("drop", (e) => {
    e.preventDefault();
    e.stopPropagation();
    node.classList.remove("over");
    const key = editor.dragKey || e.dataTransfer.getData("text/plain");
    if (key) onDrop(key);
  });
}

function accountNote(key) {
  const spec = shared.workflow.accounts.find((s) => s.key === key);
  const a = editor.accounts[(spec && spec.credential) || key];
  if (!a) return null;
  return a.configured
    ? el("span", { class: "wf-node-account ok" }, `Cuenta guardada · ${a.username_hint}`)
    : el("a", { class: "wf-node-account missing", href: "#cuentas" }, "Sin cuenta guardada");
}

function editorNode(key, stageIndex) {
  const spec = shared.workflow.accounts.find((a) => a.key === key) || { label: key, kind: "", requires: [], provides: [] };
  const inFlow = stageIndex != null;
  const invalid = inFlow && editor.problems.some((p) => p.startsWith(spec.label));
  const node = el("div", { class: `wf-node${invalid ? " invalid" : ""}`, draggable: "true", "data-key": key },
    el("div", { class: "wf-node-head" }, el("span", { class: "dot", "aria-hidden": "true" }),
      el("span", { class: "wf-node-title" }, spec.label)),
    el("span", { class: "wf-node-kind" }, spec.kind),
    spec.requires.length ? el("span", { class: "wf-node-note" }, `Necesita ${spec.requires.join(", ")}`) : null,
    spec.provides.length ? el("span", { class: "wf-node-note" }, `Entrega ${spec.provides.join(", ")}`) : null,
    accountNote(key));
  node.addEventListener("dragstart", (e) => {
    editor.dragKey = key;
    e.dataTransfer.setData("text/plain", key);
    e.dataTransfer.effectAllowed = "move";
    node.classList.add("dragged");
    document.body.classList.add("dragging");
  });
  node.addEventListener("dragend", () => {
    editor.dragKey = null;
    node.classList.remove("dragged");
    document.body.classList.remove("dragging");
    for (const n of document.querySelectorAll(".over")) n.classList.remove("over");
  });
  const tools = el("div", { class: "wf-node-tools" });
  if (inFlow) {
    const alone = editor.stages[stageIndex].length === 1;
    const first = stageIndex === 0;
    const last = stageIndex === editor.stages.length - 1;
    const left = el("button", { class: "button icon ghost", type: "button", title: "Mover a la etapa anterior",
      "aria-label": `Mover ${spec.label} antes` }, "←");
    left.disabled = first && alone;
    left.addEventListener("click", () => shift(key, -1));
    const right = el("button", { class: "button icon ghost", type: "button", title: "Mover a la etapa siguiente",
      "aria-label": `Mover ${spec.label} después` }, "→");
    right.disabled = last && alone;
    right.addEventListener("click", () => shift(key, 1));
    const remove = el("button", { class: "button icon ghost", type: "button", title: "Sacar del flujo" }, "Quitar");
    remove.addEventListener("click", () => setStages(without(key)));
    tools.append(left, right, remove);
  } else {
    const add = el("button", { class: "button icon secondary", type: "button" }, "Agregar al final");
    add.addEventListener("click", () => insertStage(key, editor.stages.length));
    tools.append(add);
  }
  node.append(tools);
  return node;
}

function gap(index) {
  const node = el("div", { class: "wf-gap", title: "Soltar aquí para crear una etapa nueva" },
    el("span", { class: "gap-hint" }, "+ etapa"));
  dropTarget(node, (key) => insertStage(key, index));
  return node;
}

function renderEditor() {
  const canvas = $("#wf-canvas");
  canvas.replaceChildren(el("div", { class: "wf-terminal" }, el("span", { class: "dot" }), "Rendición lista"));
  editor.stages.forEach((stage, i) => {
    canvas.append(gap(i));
    const col = el("div", { class: "wf-stage", "aria-label": `Etapa ${i + 1}` },
      el("div", { class: "wf-stage-head" }, el("span", {}, `Etapa ${i + 1}`), stage.length > 1 ? el("span", {}, "en paralelo") : null),
      ...stage.map((key) => editorNode(key, i)));
    dropTarget(col, (key) => moveTo(key, i));
    canvas.append(col);
  });
  canvas.append(gap(editor.stages.length), el("div", { class: "wf-terminal" }, el("span", { class: "dot" }), "Terminada"));

  const used = new Set(editor.stages.flat());
  const tray = $("#wf-tray");
  const outside = shared.workflow.accounts.filter((a) => !used.has(a.key));
  tray.replaceChildren(...(outside.length ? outside.map((a) => editorNode(a.key, null))
    : [el("span", { class: "wf-tray-empty" }, "Todas las cuentas están en el flujo. Arrastra una aquí para sacarla.")]));

  $("#wf-problems").replaceChildren(...editor.problems.map((p) => el("li", {}, p)));
  const dirty = isDirty();
  $("#wf-dirty").hidden = !dirty;
  $("#wf-save").disabled = !dirty || editor.checking || editor.problems.length > 0;
  $("#wf-discard").disabled = !dirty;
}

export async function saveEditor() {
  try {
    shared.workflow = await api("PUT", "/api/workflow", { stages: editor.stages });
    editor.saved = shared.workflow.stages.map((s) => [...s]);
    editor.stages = shared.workflow.stages.map((s) => [...s]);
    toast("Flujo guardado: se usa desde la próxima pasada", "ok");
  } catch (err) {
    if (Array.isArray(err.detail)) editor.problems = err.detail.map((d) => (typeof d === "string" ? d : d.msg));
    toast("No se pudo guardar el flujo: " + err.message, "error", 7000);
  }
  renderRoute();
  renderEditor();
}

export async function resetEditor() {
  if (!confirm("¿Volver al flujo predeterminado Colmena → Chubb?")) return;
  try {
    shared.workflow = await api("DELETE", "/api/workflow");
    toast("Flujo restaurado", "ok");
  } catch (err) {
    toast("No se pudo restaurar: " + err.message, "error");
  }
  await loadEditor();
  renderRoute();
}
