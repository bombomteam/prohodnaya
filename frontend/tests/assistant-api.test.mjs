import test from "node:test";
import assert from "node:assert/strict";
import { backendApi } from "../src/api.js";
import { session } from "../src/auth.js";

async function withFetch(handler, action) {
  const previousFetch = globalThis.fetch, previousLocation = globalThis.location, previousDev = session.dev;
  globalThis.location = new URL("http://localhost:8080/?devRole=manager");
  session.dev = true;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    const path = new URL(url).pathname;
    calls.push({ path, method: options.method, body: options.body });
    const { status = 200, value } = handler(path, options.method);
    return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
  };
  try { await action(calls); } finally {
    globalThis.fetch = previousFetch; globalThis.location = previousLocation; session.dev = previousDev;
  }
}

test("assistant answer keeps status, facts, provider and model", async () => {
  const body = { status: "unavailable", text: "Сейчас на объекте 3 человека.", facts_used: ["on_site=3"], provider: "ollama", model: "qwen2.5:1.5b" };
  await withFetch(() => ({ value: body }), async calls => {
    const answer = await backendApi.ask("Кто сейчас на объекте?");
    assert.deepEqual(answer, { status: "unavailable", answer: body.text, factsUsed: ["on_site=3"], provider: "ollama", model: "qwen2.5:1.5b" });
    assert.deepEqual(JSON.parse(calls[0].body), { question: "Кто сейчас на объекте?" });
    const summary = await backendApi.summary("2026-10-10", "2026-10-10");
    assert.equal(summary.provider, "ollama");
    assert.deepEqual(JSON.parse(calls[1].body), { date_from: "2026-10-10", date_to: "2026-10-10" });
  });
});

test("assistant status and provider switch use /assistant/status and /assistant/provider", async () => {
  await withFetch(() => ({ value: { provider: "external", model: "gpt", configured: true, available: false } }), async calls => {
    assert.deepEqual(await backendApi.assistantStatus(), { switchable: true, provider: "external", model: "gpt", configured: true, available: false });
    const next = await backendApi.setAssistantProvider("ollama");
    assert.equal(next.switchable, true);
    assert.equal(calls[1].method, "PUT");
    assert.equal(calls[1].path, "/api/assistant/provider");
    assert.deepEqual(JSON.parse(calls[1].body), { provider: "ollama" });
  });
});

test("missing status and provider endpoints (404) mean switching is unavailable, not an error", async () => {
  await withFetch(() => ({ status: 404, value: { code: "NOT_FOUND", message: "Не найдено" } }), async () => {
    assert.deepEqual(await backendApi.assistantStatus(), { switchable: false, unsupported: true });
    assert.deepEqual(await backendApi.setAssistantProvider("external"), { switchable: false, unsupported: true });
  });
});
