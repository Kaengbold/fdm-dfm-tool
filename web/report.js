// The report a person reads: one HTML file with the pictures inside (opens in any browser, prints
// to PDF). app.js collects the facts and the pictures; this only lays them out.

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

const STYLE = `
body { font: 16px/1.5 "Segoe UI", system-ui, sans-serif; color: #1f2328; max-width: 860px; margin: 32px auto; padding: 0 20px; }
h1 { font-size: 28px; margin: 0 0 4px; } h2 { font-size: 21px; margin: 36px 0 10px; padding-top: 18px; border-top: 1px solid #d5ddd7; }
h3 { font-size: 17px; margin: 0 0 6px; } p { margin: 6px 0; } .muted { color: #5b6560; font-size: 14px; }
img { width: 100%; border-radius: 8px; border: 1px solid #d5ddd7; display: block; margin: 8px 0; }
.box { border: 1px solid #d5ddd7; border-radius: 8px; padding: 14px 16px; margin: 12px 0; break-inside: avoid; }
.verdict { font-size: 19px; font-weight: 700; } .ok { background: #e7f3ec; } .warn { background: #fbf0d9; } .bad { background: #fbe4e1; }
.sev { float: right; font-size: 13px; font-weight: 700; color: #5b6560; }
ul { margin: 6px 0; padding-left: 20px; } table { border-collapse: collapse; width: 100%; font-size: 15px; }
td, th { text-align: left; padding: 6px 8px; border-bottom: 1px solid #e3e9e4; vertical-align: top; } td.why { color: #5b6560; font-size: 13px; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; } .two img { margin: 0; }
@media print { body { margin: 0; } h2 { break-after: avoid; } }`;

/** ``r``: {title, date, setup, verdict:{kind, head, text}, overview, orientation:{label, reasons}, stats,
 * problems:[{title, severity, picture, details, why, fixes:[{kind, text}]}], strength?, warp?, design?,
 * settings:[{label, value, reason}]} — see app.js downloadReport. */
export function reportHtml(r) {
  const section = (title, ...body) => `<h2>${esc(title)}</h2>${body.join("")}`;
  const picture = (src, alt) => (src ? `<img src="${src}" alt="${esc(alt)}">` : "");
  const problems = r.problems.length
    ? r.problems.map((p) => `<div class="box"><span class="sev">${esc(p.severity)}</span><h3>${esc(p.title)}</h3>
        ${picture(p.picture, p.title)}
        ${p.details.map((d) => `<p><b>${esc(d)}</b></p>`).join("")}
        <p class="muted">${esc(p.why)}</p>
        ${p.fixes.length ? `<ul>${p.fixes.map((f) => `<li><b>${esc(f.kind)}:</b> ${esc(f.text)}</li>`).join("")}</ul>` : ""}</div>`).join("")
    : `<p>No problems found in this orientation.</p>`;
  const result = (x, title) => x && section(title,
    `<div class="box ${x.kind}"><p class="verdict">${esc(x.head)}</p>${x.lines.map((l) => `<p>${esc(l)}</p>`).join("")}</div>`,
    picture(x.picture, title), x.note ? `<p class="muted">${esc(x.note)}</p>` : "");
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(r.title)}</title><style>${STYLE}</style></head><body>
<h1>${esc(r.title)}</h1>
<p class="muted">${esc(r.setup)} · ${esc(r.date)}</p>
<div class="box ${r.verdict.kind}"><p class="verdict">${esc(r.verdict.head)}</p><p>${esc(r.verdict.text)}</p></div>
${section("How it lies on the bed", picture(r.overview, "The part on the bed"),
    `<p><b>${esc(r.orientation.label)}</b></p><ul>${r.orientation.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`,
    `<p>${esc(r.stats)}</p>`)}
${section("Problems", problems)}
${result(r.strength, "Will it hold?") || ""}
${result(r.warp, "Warping") || ""}
${r.design ? section("Design changes", picture(r.design.picture, "Design changes"),
    `<p class="muted">Green = plastic added, red = cut away.</p><ul>${r.design.items.map((d) => `<li><b>${esc(d.title)}</b> — ${esc(d.description)}</li>`).join("")}</ul>`) : ""}
${r.settings.length ? section("Slicer settings", "<p class=\"muted\">What to change from your slicer's defaults (already set in the 3MF download).</p>",
    `<table><tr><th>Setting</th><th>Value</th><th>Why</th></tr>${r.settings.map((s) => `<tr><td>${esc(s.label)}</td><td><b>${esc(s.value)}</b></td><td class="why">${esc(s.reason)}</td></tr>`).join("")}</table>`) : ""}
<p class="muted" style="margin-top:36px">Made with FDM Check. Times, strength and warping are rough estimates: check the first print.</p>
</body></html>`;
}
