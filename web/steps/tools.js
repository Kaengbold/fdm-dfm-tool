// More tools (T-564, plan §15.1 decision 9), opened from Done: the old app's extras, one at a time behind a switch.
// Zones and hidden gaps, Split the part, Copies on the plate (all three: "your slicer can do this too"), Electronics
// box heat and Versions. Each tool is a module in steps/tools/ with render(ctx) and, where it draws on the part,
// show / hide (and onPick for the ones that take a click). In the flow after Done, in the rail only while open.
import { SegmentedControl } from "../ui/choice.js";
import { h } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { registerStep } from "../shell.js";
import { t } from "../i18n.js";
import * as zones from "./tools/zones.js";
import * as split from "./tools/split.js";
import * as copies from "./tools/copies.js";
import * as box from "./tools/box.js";
import * as versions from "./tools/versions.js";

export const TOOLS = { zones, split, copies, box, versions };
let tool = "zones";
let wired = false;

/** Open More tools on one tool (Done's list, Problems' fixes). */
export function openTool(name, go) {
  if (TOOLS[name]) tool = name;
  go("tools");
}

function clear(services) {
  services.stage.clearGhosts();
  services.stage.hideField();
}

function wire(store, services) {
  if (wired) return;
  wired = true;
  services.picker?.on("pick", (hit) => {
    if (store.get().step !== "tools" || !hit) return;
    TOOLS[tool].onPick?.(hit, store, services);
  });
}

registerStep("tools", {
  render(ctx) {
    const { body, store, go, services } = ctx;
    wire(store, services);
    if (!store.get().option) {
      body.append(h("p", { class: "ui-hint" }, t("sim.noOption")), Button({ label: t("sim.toOptions"), onClick: () => go("options") }));
      return;
    }
    body.append(h("div", { class: "tools-tabs" }, SegmentedControl({ label: t("tools.which"), size: "small", value: tool, options: Object.keys(TOOLS).map((k) => ({ value: k, label: t(`tools.tab.${k}`) })),
      onChange: (v) => { clear(services); tool = v; services.rerender(); } })));
    if (!TOOLS[tool].show) clear(services);
    TOOLS[tool].render(ctx);
  },
  leave({ services }) { clear(services); },
});
