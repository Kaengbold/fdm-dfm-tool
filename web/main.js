// Entry of the new UI (web/new.html, T-530). The theme follows the saved switch (light by default, §16.6).
import { startShell } from "./shell.js";
import "./steps/setup.js";
import "./steps/environment.js";

try {
  if (localStorage.getItem("fdm-theme") === "dark") document.documentElement.dataset.theme = "dark";
} catch { /* storage blocked: light */ }
const app = startShell(document.getElementById("app"));
globalThis.fdmUI = app; // for the browser tests and for agents driving the page
document.body.dataset.ready = "1";
