// Rendiciones view: wires the list, filters, side panel, batch review and uploads.

import { $ } from "../core.js";
import { initBatch } from "./batch.js";
import { initToolbar } from "./filters.js";
import { initList, renderList } from "./list.js";
import { hasDrafts, initPanel } from "./panel.js";
import { notify, refresh, sessionQuery, syncFromUrl } from "./store.js";
import { initUpload } from "./upload.js";

export function initRendiciones() {
  initToolbar();
  initList();
  initPanel();
  initBatch();
  initUpload();
  $("#refresh").addEventListener("click", () => refresh());
  window.addEventListener("focus", () => {
    if (!$("#view-rendiciones").hidden && !hasDrafts()) refresh();
  });
  renderList(); // loading skeleton until the first fetch lands
}

/** Called on every navigation into (or within) the Rendiciones view. */
export function showRendiciones({ entering }) {
  // Coming back from another tab with a bare "#rendiciones": restore the last filters.
  if (entering && location.hash.replace(/^#/, "") === "rendiciones" && sessionQuery && sessionQuery !== location.hash) {
    history.replaceState(null, "", sessionQuery);
  }
  const changed = syncFromUrl();
  if (entering) refresh();
  else if (changed) notify();
}
