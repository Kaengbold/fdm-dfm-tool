// Shared bits of Done's More tools (T-564): a busy line with Cancel, and the build-frame boxes (zones, hidden gaps,
// support boxes) drawn on the part.
import { h } from "../../ui/dom.js";
import { Button } from "../../ui/button.js";
import { Cancelled } from "../../engine.js";
import { t } from "../../i18n.js";

/** Rotation R with R · up = +Z, the same as the engine's orientation.rotation_to_z (Rodrigues). Rows of a 3×3. */
export function rotationToZ(up) {
  const n = Math.hypot(...up) || 1;
  const a = up.map((v) => v / n);
  const v = [a[1], -a[0], 0]; // a × z
  const c = a[2];
  if (Math.hypot(...v) < 1e-12 || 1 + c < 1e-12) return c > 0 ? [[1, 0, 0], [0, 1, 0], [0, 0, 1]] : [[1, 0, 0], [0, -1, 0], [0, 0, -1]];
  const vx = [[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]];
  const k = 1 / (1 + c);
  return [0, 1, 2].map((i) => [0, 1, 2].map((j) => (i === j ? 1 : 0) + vx[i][j] + k * [0, 1, 2].reduce((s, m) => s + vx[i][m] * vx[m][j], 0)));
}

const CORNERS = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]];
const FACES = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]];

/** A build-frame box (``lo`` / ``hi`` as zone_at / volume_at return them) as triangles in part coordinates. */
export function boxSoup(lo, hi, up) {
  const r = rotationToZ(up);
  const pts = CORNERS.map((c) => {
    const b = c.map((u, i) => lo[i] + u * (hi[i] - lo[i]));
    return [0, 1, 2].map((i) => r[0][i] * b[0] + r[1][i] * b[1] + r[2][i] * b[2]); // part = Rᵀ · built
  });
  return new Float32Array(FACES.flatMap((f) => f.flatMap((i) => pts[i])));
}

/** Run ``fn`` with a busy line; errors land in ``state.error``. ``state``: {busy, error}. */
export async function run(state, text, fn, services) {
  if (state.busy) return;
  state.busy = text;
  state.error = null;
  services.rerender();
  try {
    await fn();
  } catch (e) {
    if (!(e instanceof Cancelled)) state.error = e.message;
  } finally {
    state.busy = null;
    services.rerender();
  }
}

export function busyLine(state, services) {
  return [
    state.busy && h("div", { class: "sim-running", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), h("span", {}, state.busy),
      Button({ label: t("ui.cancel"), variant: "ghost", onClick: () => services.engine.cancel() })),
    state.error && h("p", { class: "drop-error", role: "alert" }, state.error),
  ].filter(Boolean); // Element.append would write a null as text
}

export const f32 = (b) => new Float32Array(new Uint8Array(b).slice().buffer);
export const baseName = (s) => (s.part?.name ?? "part").replace(/\.(stl|3mf|step|stp)$/i, "");
