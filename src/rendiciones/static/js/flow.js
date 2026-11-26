// Per-rendición progress strip (one node per account) and history wording.

import { accountLabel, el, fmtDateTime, fmtMoney, fmtShortDate } from "./core.js";

const DOT_GLYPH = { aprobada: "✓", rechazada: "×", error: "!" };

const breakdownExtras = (n) => (n.breakdown || []).slice(1);

function nodeMeta(n) {
  const parts = [];
  if (n.request_id) parts.push(`N° ${n.request_id}`);
  if (n.monto != null) parts.push(`${n.monto_label} ${fmtMoney(n.monto)}`);
  for (const b of breakdownExtras(n)) parts.push(`incluye ${b.label.toLowerCase()} ${fmtMoney(b.monto)}`);
  const when = n.resolved_at || n.submitted_at;
  if (when) parts.push(fmtShortDate(when));
  return parts;
}

function nodeTitle(n) {
  const lines = [`${n.label}: ${n.state_label}`];
  if (n.request_id) lines.push(`Solicitud ${n.request_id}`);
  if (n.submitted_at) lines.push(`Enviada ${fmtDateTime(n.submitted_at)}`);
  if (n.resolved_at) lines.push(`Resuelta ${fmtDateTime(n.resolved_at)}`);
  if (n.monto != null) lines.push(`${n.monto_label}: ${fmtMoney(n.monto)}`);
  for (const b of n.breakdown || []) lines.push(`  ${b.label}: ${fmtMoney(b.monto)}`);
  return lines.join("\n");
}

export function renderFlow(container, nodes) {
  const stages = [];
  for (const n of nodes) {
    const index = n.stage == null ? 1000 + stages.length : n.stage;
    let stage = stages.find((s) => s.index === index);
    if (!stage) stages.push((stage = { index, nodes: [] }));
    stage.nodes.push(n);
  }
  stages.sort((a, b) => a.index - b.index);
  container.replaceChildren();
  stages.forEach((stage, i) => {
    const hasNext = i < stages.length - 1;
    const done = stage.nodes.every((n) => n.state === "aprobada");
    if (i) {
      const prevDone = stages[i - 1].nodes.every((n) => n.state === "aprobada");
      container.append(el("div", { class: `flow-link${prevDone ? " done" : ""}`, "aria-hidden": "true" }));
    }
    const col = el("div", { class: `flow-stage${hasNext ? " has-next" : ""}${done ? " done" : ""}` });
    for (const n of stage.nodes) {
      const meta = nodeMeta(n).flatMap((part, k) => (k ? [" · ", el("span", { class: "nowrap" }, part)] : [el("span", { class: "nowrap" }, part)]));
      col.append(el("div", {
        class: `flow-node state-${n.state}${n.in_workflow ? "" : " outside"}`,
        role: "listitem", title: nodeTitle(n),
      },
      el("span", { class: "rail", "aria-hidden": "true" }, el("span", { class: "dot" }, DOT_GLYPH[n.state] || "")),
      el("span", { class: "who" }, n.label),
      el("span", { class: "state" }, n.state_label),
      meta.length ? el("span", { class: "meta" }, ...meta) : null));
    }
    container.append(col);
  });
}

export function eventExtra(ev) {
  if (ev.kind === "dry_run" && ev.detail.portal) return ` en ${accountLabel(ev.detail.portal)}`;
  if (!ev.kind.startsWith("abono_") || !ev.detail.account) return "";
  const who = ` (${accountLabel(ev.detail.account).replace(/^Abono /, "")})`;
  if (!ev.detail.excedente) return who;
  return `${who}: ${fmtMoney(ev.detail.monto)}, incluye ${fmtMoney(ev.detail.excedente)} de excedentes`;
}
