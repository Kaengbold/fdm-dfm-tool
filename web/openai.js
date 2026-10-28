// "Ask AI" with OpenAI (bring your own platform.openai.com API key). Same tools and
// interface as the Anthropic assistant; plain fetch against the Responses API. The
// conversation lives on OpenAI's side (previous_response_id), so only new turns are sent.
import { SYSTEM, TOOLS } from "./assistant.js";

const API = "https://api.openai.com/v1";

/** Chat models this key can use, newest first (the list endpoint has no capability flags). */
export async function listOpenAIModels(apiKey) {
  const res = await fetch(`${API}/models`, { headers: { authorization: `Bearer ${apiKey}` } });
  if (!res.ok) throw new Error(`OpenAI: ${await errorText(res)}`);
  const { data = [] } = await res.json();
  return data
    .map((m) => m.id)
    .filter((id) => /^(gpt-|o\d)/.test(id) && !/audio|realtime|transcribe|tts|image|search|embedding|moderation|instruct/i.test(id))
    .sort((a, b) => a.startsWith("o") - b.startsWith("o") || b.localeCompare(a, undefined, { numeric: true }))
    .map((id) => ({ id, label: id }));
}

const FUNCTIONS = TOOLS.map((t) => ({ type: "function", name: t.name, description: t.description, parameters: t.input_schema }));

async function errorText(res) {
  try {
    return (await res.json()).error?.message ?? res.statusText;
  } catch {
    return res.statusText;
  }
}

export class OpenAIAssistant {
  constructor(actions) {
    this.actions = actions;
    this.previous = null; // id of the last response: the conversation so far
  }

  reset() {
    this.previous = null;
  }

  async ask(text, { apiKey, model }, onEvent) {
    const start = this.previous;
    try {
      await this.#loop([{ role: "user", content: text }], apiKey, model, onEvent);
    } catch (err) {
      this.previous = start; // a half-finished tool round would leave the conversation invalid
      throw err;
    }
  }

  async #loop(input, apiKey, model, onEvent) {
    for (let step = 0; step < 12; step++) {
      const res = await fetch(`${API}/responses`, {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${apiKey}` },
        body: JSON.stringify({ model, instructions: SYSTEM, input, tools: FUNCTIONS, previous_response_id: this.previous ?? undefined }),
      });
      if (!res.ok) throw new Error(`OpenAI: ${await errorText(res)}`);
      const data = await res.json();
      this.previous = data.id;
      const output = data.output ?? [];
      for (const item of output) {
        if (item.type !== "message") continue;
        for (const c of item.content ?? []) {
          if (c.type === "output_text" && c.text.trim()) onEvent({ type: "text", text: c.text });
          if (c.type === "refusal") onEvent({ type: "text", text: c.refusal });
        }
      }
      const calls = output.filter((item) => item.type === "function_call");
      if (!calls.length) {
        if (data.status === "incomplete") onEvent({ type: "text", text: "(Answer cut off — try a shorter question.)" });
        return;
      }
      input = [];
      for (const call of calls) {
        onEvent({ type: "tool", name: call.name });
        let result;
        try {
          result = { result: (await this.actions[call.name](JSON.parse(call.arguments || "{}"))) ?? { ok: true } };
        } catch (err) {
          result = { error: String(err.message ?? err) };
        }
        input.push({ type: "function_call_output", call_id: call.call_id, output: JSON.stringify(result) });
      }
    }
    onEvent({ type: "text", text: "(Stopped after too many steps.)" });
  }
}
