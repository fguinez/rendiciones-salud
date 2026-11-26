// Entry point: top-level routing between the Rendiciones, Flujo and Cuentas views.

import { $, loadMeta } from "./js/core.js";
import { loadAccounts } from "./js/cuentas.js";
import { dropTarget, editor, isDirty, loadEditor, resetEditor, saveEditor, setStages, without } from "./js/flujo.js";
import { loadPipeline, loadWorkflow, startPipeline } from "./js/pipeline.js";
import { initRendiciones, showRendiciones } from "./js/rendiciones/index.js";
import { parseHash } from "./js/rendiciones/store.js";

const VIEWS = ["rendiciones", "flujo", "cuentas"];
let currentView = null;

function route() {
  const { name } = parseHash();
  const view = VIEWS.includes(name) ? name : "rendiciones";
  if (currentView === "flujo" && view !== "flujo" && isDirty() &&
      !confirm("Hay cambios sin guardar en el flujo. ¿Salir y descartarlos?")) {
    history.replaceState(null, "", "#flujo");
    return;
  }
  if (currentView === "flujo" && view !== "flujo") editor.stages = editor.saved.map((s) => [...s]);
  const entering = currentView !== view;
  currentView = view;
  for (const name of VIEWS) $(`#view-${name}`).hidden = name !== view;
  for (const link of document.querySelectorAll(".tabs a")) {
    if (link.dataset.view === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  document.body.dataset.view = view;
  if (view === "cuentas") { if (entering) loadAccounts(); }
  else if (view === "flujo") { if (entering) loadEditor(); }
  else {
    if (entering) loadPipeline();
    showRendiciones({ entering });
  }
}

async function init() {
  $("#run-dry").addEventListener("click", (e) => startPipeline("dry_run", e.currentTarget));
  $("#run-submit").addEventListener("click", (e) => startPipeline("submit", e.currentTarget));
  $("#wf-save").addEventListener("click", saveEditor);
  $("#wf-discard").addEventListener("click", () => { editor.problems = []; setStages(editor.saved.map((s) => [...s])); });
  $("#wf-reset").addEventListener("click", resetEditor);
  dropTarget($("#wf-tray"), (key) => setStages(without(key)));
  window.addEventListener("beforeunload", (e) => { if (isDirty()) e.preventDefault(); });

  initRendiciones();
  // pushState does not fire hashchange; listen to both and let route() be idempotent.
  window.addEventListener("hashchange", route);
  window.addEventListener("popstate", route);
  await Promise.all([loadMeta(), loadWorkflow()]);
  route();
}

init();
