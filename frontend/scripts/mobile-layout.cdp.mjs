// Optional local browser QA: start Edge/Chrome with --remote-debugging-port=9223
// and serve the app on http://localhost:8080 before running this script.
import assert from "node:assert/strict";

const port = process.env.CDP_PORT || "9223";
const appUrl = process.env.QA_URL || "http://localhost:8080/";
const target = await fetch(`http://127.0.0.1:${port}/json/new?${appUrl}`, { method: "PUT" }).then(r => r.json());
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });

let nextId = 0;
const pending = new Map();
socket.onmessage = ({ data }) => {
  const message = JSON.parse(data);
  if (!message.id) return;
  const promise = pending.get(message.id);
  if (!promise) return;
  pending.delete(message.id);
  message.error ? promise.reject(new Error(message.error.message)) : promise.resolve(message.result);
};
function send(method, params = {}) {
  const id = ++nextId;
  return new Promise((resolve, reject) => { pending.set(id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params })); });
}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function evaluate(expression) {
  const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.result.value;
}

try {
  await send("Page.enable");
  await send("Runtime.enable");
  const failures = [];
  for (const width of [320, 360, 390, 768, 1024]) {
    await send("Emulation.setDeviceMetricsOverride", { width, height: 844, deviceScaleFactor: 1, mobile: width < 768 });
    await send("Page.navigate", { url: appUrl });
    await sleep(900);
    for (const page of ["overview", "employees", "gate", "worker", "analytics", "assistant", "journal"]) {
      await evaluate(`document.querySelector('[data-page="${page}"]').click()`);
      await sleep(160);
      const layout = await evaluate(`({viewport: innerWidth, scrollWidth: document.documentElement.scrollWidth, title: document.querySelector('h1')?.textContent, menuVisible: getComputedStyle(document.querySelector('.mobile-nav')).display !== 'none'})`);
      console.log(`${width}px ${page}: viewport=${layout.viewport}, content=${layout.scrollWidth}`);
      if (layout.scrollWidth > layout.viewport + 1) failures.push(`${width}px ${page}: horizontal overflow ${layout.scrollWidth - layout.viewport}px`);
    }
    if (width < 768) {
      const menu = await evaluate(`(document.querySelector('.mobile-menu-button').click(), !!document.querySelector('[role="dialog"][aria-label="Разделы приложения"]'))`);
      if (!menu) failures.push(`${width}px: mobile menu did not open`);
      const destination = await evaluate(`(document.querySelector('.mobile-menu-item[data-page="employees"]').click(), document.querySelector('h1')?.textContent)`);
      if (destination !== "Сотрудники") failures.push(`${width}px: mobile menu navigation failed`);
    }
  }
  assert.deepEqual(failures, []);
} finally {
  socket.close();
  await fetch(`http://127.0.0.1:${port}/json/close/${target.id}`).catch(() => {});
}
