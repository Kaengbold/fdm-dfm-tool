// "Ask AI": a bring-your-own-key assistant that can operate the tool.
// The Anthropic SDK runs in the browser and talks to api.anthropic.com directly;
// the key never touches any other server. Tools map onto the same actions as the UI.
const SDK_URL = "https://cdn.jsdelivr.net/npm/@anthropic-ai/sdk@1.3.0/+esm";

export const MODELS = [
  { id: "claude-haiku-5-5", label: "Claude Haiku 5.5 (cheap, fast)" },
  { id: "claude-sonnet-5-5", label: "Claude Sonnet 5.5" },
  { id: "claude-opus-5-5", label: "Claude Opus 5.5 (best)" },
];
// Server-side refusal fallback (routes a declined request to another model); not offered for Haiku.
const FALLBACK_MODELS = new Set(["claude-sonnet-5-5", "claude-opus-5-5"]);

export const SYSTEM = `You are the assistant inside FDM DFM Tool, a design-for-3D-printing (FDM) helper for hobbyists.
The user has loaded one part. Use the tools to look at the current analysis, compare orientations,
run rough strength checks, and propose design or slicer changes. Explain in plain, friendly language
and keep answers short. All numbers from the tool are rough estimates: say so when it matters.
Change things (orientation, loads, settings, design edits) only when the user asks for it or agrees.
Before applying design changes, say which ones and why.`;

export const TOOLS = [
  {
    name: "get_current_analysis",
    description: "Current part, printer/material/nozzle, mode, selected orientation, findings, print estimate, recommended settings, loads and the last strength result.",
    input_schema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "list_orientations",
    description: "The ranked orientation options with their reasons. Index 0 is the recommended one.",
    input_schema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "select_orientation",
    description: "Switch the view and analysis to orientation option `index` (from list_orientations). Returns the new analysis.",
    input_schema: { type: "object", properties: { index: { type: "integer", minimum: 0 } }, required: ["index"], additionalProperties: false },
  },
  {
    name: "list_faces",
    description: "Faces of the part (id, flat/curved, area mm², outward normal, centroid in part coordinates). Use ids to define loads.",
    input_schema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "set_loads",
    description: "Replace the loads. Needs at least one fixed face and one force. Forces with different `case` numbers are separate situations; the weakest case decides.",
    input_schema: {
      type: "object",
      properties: {
        loads: {
          type: "array",
          items: {
            type: "object",
            properties: {
              region: { type: "integer", description: "Face id from list_faces" },
              kind: { type: "string", enum: ["fixed", "force"] },
              kg: { type: "number", description: "Load in kg (force only)" },
              direction: { type: "string", enum: ["push", "pull", "down"], description: "push into the face, pull it away, or hang down (-Z as modelled)" },
              case: { type: "integer", minimum: 1, description: "Load case number (force only, default 1)" },
            },
            required: ["region", "kind"],
            additionalProperties: false,
          },
        },
      },
      required: ["loads"],
      additionalProperties: false,
    },
  },
  {
    name: "check_strength",
    description: "Run the rough strength simulation for the current orientation and loads. Returns safety factor, verdict and failure mode.",
    input_schema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "strength_options",
    description: "Safety factor with more walls, more infill or the strongest orientation (takes a few seconds).",
    input_schema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "suggest_design_changes",
    description: "Detected holes and suggested geometry edits for the current orientation: teardrop holes and corner chamfers (cut away), 45° fills under ledges, sacrificial layers over holes that start in mid-air, thicker walls (added).",
    input_schema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "tag_feature",
    description: "Confirm or correct a detected feature (region from suggest_design_changes): tag \"hole\", \"boss\" or \"none\" if it isn't one (e.g. a rounded edge); \"forget\" removes the answer. Ask the user about features with confidence \"low\". Rejected holes get no edits and no hole findings.",
    input_schema: {
      type: "object",
      properties: { region: { type: "integer" }, tag: { type: "string", enum: ["hole", "boss", "none", "forget"] } },
      required: ["region", "tag"],
      additionalProperties: false,
    },
  },
  {
    name: "apply_design_changes",
    description: "Apply the given suggested edits to the part (ids from suggest_design_changes) and re-analyse it.",
    input_schema: { type: "object", properties: { ids: { type: "array", items: { type: "string" } } }, required: ["ids"], additionalProperties: false },
  },
  {
    name: "set_slicer_settings",
    description: "Override recommended slicer settings (OrcaSlicer keys, string values), e.g. {\"wall_loops\": \"4\"}. Used in the 3MF export.",
    input_schema: {
      type: "object",
      properties: { settings: { type: "object", additionalProperties: { type: "string" } } },
      required: ["settings"],
      additionalProperties: false,
    },
  },
];

let sdk = null;

export class Assistant {
  /** ``actions``: the app's functions behind each tool (see app.js). */
  constructor(actions) {
    this.actions = actions;
    this.messages = [];
  }

  reset() {
    this.messages = [];
  }

  /** Send a user message; ``onEvent`` gets {type: "text"|"tool", ...} as the turn progresses. */
  async ask(text, { apiKey, model }, onEvent) {
    sdk ??= import(SDK_URL);
    const { default: Anthropic } = await sdk;
    const client = new Anthropic({ apiKey, dangerouslyAllowBrowser: true });
    const start = this.messages.length;
    this.messages.push({ role: "user", content: text });
    try {
      await this.#loop(client, model, onEvent);
    } catch (err) {
      this.messages.length = start; // keep the history valid (no dangling user turn or tool call)
      throw err;
    }
  }

  async #loop(client, model, onEvent) {
    for (let step = 0; step < 12; step++) {
      const params = { model, max_tokens: 16000, system: SYSTEM, tools: TOOLS, messages: this.messages };
      const response = FALLBACK_MODELS.has(model)
        ? await client.beta.messages.create({ ...params, betas: ["server-side-fallback-2026-07-01"], fallbacks: "default" })
        : await client.messages.create(params);
      // Append the full content unchanged (thinking blocks must be passed back as-is).
      this.messages.push({ role: "assistant", content: response.content });
      for (const block of response.content) if (block.type === "text" && block.text.trim()) onEvent({ type: "text", text: block.text });

      if (response.stop_reason === "refusal") return onEvent({ type: "text", text: "(The model declined this request.)" });
      if (response.stop_reason === "max_tokens") return onEvent({ type: "text", text: "(Answer cut off — try a shorter question.)" });
      if (response.stop_reason === "pause_turn") continue;
      const uses = response.content.filter((b) => b.type === "tool_use");
      if (response.stop_reason !== "tool_use" || !uses.length) return;

      const results = [];
      for (const use of uses) {
        onEvent({ type: "tool", name: use.name });
        try {
          const out = await this.actions[use.name](use.input ?? {});
          results.push({ type: "tool_result", tool_use_id: use.id, content: JSON.stringify(out ?? { ok: true }) });
        } catch (err) {
          results.push({ type: "tool_result", tool_use_id: use.id, content: String(err.message ?? err), is_error: true });
        }
      }
      // All results of one turn go back in a single user message.
      this.messages.push({ role: "user", content: results });
    }
    onEvent({ type: "text", text: "(Stopped after too many steps.)" });
  }
}
