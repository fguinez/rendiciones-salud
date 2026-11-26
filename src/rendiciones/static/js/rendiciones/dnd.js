// Documents being dragged inside the app (side panel → list row, batch group → group).
// dataTransfer cannot be read during dragover, so the ids live here.

export const DOC_MIME = "application/x-rendiciones-docs";
export const drag = { docIds: null };

export function startDocDrag(e, docIds, label) {
  drag.docIds = docIds;
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData(DOC_MIME, JSON.stringify(docIds));
  e.dataTransfer.setData("text/plain", label);
  document.body.classList.add("dragging-docs");
}

export function endDocDrag() {
  drag.docIds = null;
  document.body.classList.remove("dragging-docs");
  for (const n of document.querySelectorAll(".drop-over")) n.classList.remove("drop-over");
}
