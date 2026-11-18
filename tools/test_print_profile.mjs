// Run with node tools/test_print_profile.mjs; the browser module has no Node dependencies.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { gunzipSync } from "node:zlib";

const source = await readFile(new URL("../web/mypresets.js", import.meta.url), "utf8");
const { fromPrintProfile } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const profiles = JSON.parse(gunzipSync(await readFile(new URL("../core/fdmcore/data/orca_profiles.json.gz", import.meta.url)))).profiles;
const [name, parent] = Object.entries(profiles).find(([, p]) => p.kind === "process" && p.data.layer_height);
const orca = fromPrintProfile(JSON.stringify({ inherits: name, wall_loops: "4" }), false, profiles);
assert.equal(orca.values.wall_loops, "4");
assert.equal(orca.values.layer_height, parent.data.layer_height);
assert.equal(orca.missingParent, null);
const missing = fromPrintProfile('{"inherits":"unknown","wall_loops":"4"}', false, profiles);
assert.equal(missing.missingParent, "unknown");
assert.ok(missing.missing.includes("layer height"));
const prusa = fromPrintProfile("perimeters = 4\nfill_density = 15%\nsupport_material = 1\nsupport_material_style = organic\nbrim_width = 5\nbridge_speed = 30\n", true);
assert.deepEqual(prusa.values, { wall_loops: "4", sparse_infill_density: "15%", enable_support: "1", support_type: "tree(auto)", brim_width: "5", bridge_speed: "30", brim_type: "outer_only" });
console.log("Print profile parsing passed");
