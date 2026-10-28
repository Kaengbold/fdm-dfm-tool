// "Ask AI" with Google Gemini (bring your own AI Studio API key). Same tools and
// interface as the Anthropic assistant; plain fetch against the Gemini REST API.
import { SYSTEM, TOOLS } from "./assistant.js";

const API = "https://generativelanguage.googleapis.com/v1beta";

/** Gemini models this key can use for chat with function calling, best guess first. */
export async function listGeminiModels(apiKey) {
  const res = await fetch(`${API}/models?pageSize=200`, { headers: { "x-goog-api-key": apiKey } });
  if (!res.ok) throw new Error(`Gemini: ${await errorText(res)}`);
  const { models = [] } = await res.json();
  return models
    .filter((m) => m.supportedGenerationMethods?.includes("generateContent") && /gemini/i.test(m.name) && !/embedding|image|tts|audio|live/i.test(m.name))
    .map((m) => ({ id: m.name.replace(/^models\//, ""), label: m.displayName || m.name }))
    .sort((a, b) => b.id.localeCompare(a.id, undefined, { numeric: true }));
}

// Gemini's schema dialect (OpenAPI subset) rejects `additionalProperties`.
function toGeminiSchema(schema) {
  if (Array.isArray(schema)) return schema.map(toGeminiSchema);
  if (!schema || typeof schema !== "object") return schema;
  const out = {};
  for (const [k, v] of Object.entries(schema)) if (k !== "additionalProperties") out[k] = toGeminiSchema(v);
  return out;
}

const DECLARATIONS = TOOLS.map((t) => {
  const params = toGeminiSchema(t.input_schema);
  // Parameterless tools: omit `parameters` (an empty object schema is rejected by some versions).
  return Object.keys(params.properties ?? {}).length
    ? { name: t.name, description: t.description, parameters: params }
    : { name: t.name, description: t.description };
});

async function errorText(res) {
  try {
    return (await res.json()).error?.message ?? res.statusText;
  } catch {
    return res.statusText;
  }
}

export class GeminiAssistant {
  constructor(actions) {
    this.actions = actions;
    this.contents = [];
  }

  reset() {
    this.contents = [];
  }

  async ask(text, { apiKey, model }, onEvent) {
    const start = this.contents.length;
    this.contents.push({ role: "user", parts: [{ text }] });
    try {
      await this.#loop(apiKey, model, onEvent);
    } catch (err) {
      this.contents.length = start; // keep the history valid
      throw err;
    }
  }

  async #loop(apiKey, model, onEvent) {
    for (let step = 0; step < 12; step++) {
      const res = await fetch(`${API}/models/${encodeURIComponent(model)}:generateContent`, {
        method: "POST",
        headers: { "content-type": "application/json", "x-goog-api-key": apiKey },
        body: JSON.stringify({
          systemInstruction: { parts: [{ text: SYSTEM }] },
          contents: this.contents,
          tools: [{ functionDeclarations: DECLARATIONS }],
        }),
      });
      if (!res.ok) throw new Error(`Gemini: ${await errorText(res)}`);
      const data = await res.json();
      const candidate = data.candidates?.[0];
      if (!candidate?.content) {
        const reason = data.promptFeedback?.blockReason ?? candidate?.finishReason ?? "no answer";
        return onEvent({ type: "text", text: `(Gemini returned no answer: ${reason})` });
      }
      // Echo the model turn back unchanged — it carries the thought signatures Gemini needs.
      this.contents.push(candidate.content);
      const parts = candidate.content.parts ?? [];
      for (const p of parts) if (p.text && !p.thought && p.text.trim()) onEvent({ type: "text", text: p.text });

      const calls = parts.filter((p) => p.functionCall).map((p) => p.functionCall);
      if (!calls.length) {
        if (candidate.finishReason === "MAX_TOKENS") onEvent({ type: "text", text: "(Answer cut off — try a shorter question.)" });
        return;
      }
      const responses = [];
      for (const call of calls) {
        onEvent({ type: "tool", name: call.name });
        let response;
        try {
          response = { result: (await this.actions[call.name](call.args ?? {})) ?? { ok: true } };
        } catch (err) {
          response = { error: String(err.message ?? err) };
        }
        // `response` must be an object; results of all calls go back in one turn.
        responses.push({ functionResponse: { name: call.name, ...(call.id ? { id: call.id } : {}), response } });
      }
      this.contents.push({ role: "user", parts: responses });
    }
    onEvent({ type: "text", text: "(Stopped after too many steps.)" });
  }
}
