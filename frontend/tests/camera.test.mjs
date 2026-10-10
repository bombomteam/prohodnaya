import test from "node:test";
import assert from "node:assert/strict";
import { CAMERA_TEXT, createCameraLive } from "../src/camera.js";
import { ApiError, backendApi, request } from "../src/api.js";
import { configureAuthTransport, demoLogin, logout, session } from "../src/auth.js";
import { config } from "../config.js";

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(check, message, timeoutMs = 2000) {
  const started = Date.now();
  while (!check()) {
    if (Date.now() - started > timeoutMs) throw new Error(`Не дождались: ${message}`);
    await sleep(2);
  }
}
const jpeg = () => new Blob([new Uint8Array([0xff, 0xd8, 0xff, 0xd9])], { type: "image/jpeg" });

// Минимальная замена DOM: блок #camera-live с картинкой, статусом и плашкой.
function fakeNode() {
  const attrs = new Map();
  return { hidden: false, textContent: "", className: "",
    getAttribute: name => attrs.has(name) ? attrs.get(name) : null, setAttribute: (name, value) => attrs.set(name, String(value)), removeAttribute: name => attrs.delete(name) };
}
function fakePage() {
  const parts = { "#camera-live-img": fakeNode(), "#camera-live-status": fakeNode(), "#camera-live-status-text": fakeNode(), "#camera-live-note": fakeNode() };
  const element = { querySelector: selector => parts[selector] || null };
  let mounted = true;
  const root = { querySelector: selector => selector === "#camera-live" && mounted ? element : null };
  return { root, element, parts, unmount() { mounted = false; } };
}
function fakeEnv() {
  const listeners = new Map();
  const doc = {
    visibilityState: "visible",
    addEventListener: (type, fn) => listeners.set(type, fn),
    removeEventListener: (type, fn) => { if (listeners.get(type) === fn) listeners.delete(type); },
    fire: type => listeners.get(type)?.(),
    listeners,
  };
  const created = [], revoked = [];
  const urls = { createObjectURL: () => { const url = `blob:test/${created.length + 1}`; created.push(url); return url; }, revokeObjectURL: url => revoked.push(url) };
  const observers = [];
  class Observer {
    constructor(callback) { this.callback = callback; this.targets = []; this.disconnected = false; observers.push(this); }
    observe(target) { this.targets.push(target); this.callback([{ target, isIntersecting: true }]); }
    disconnect() { this.disconnected = true; this.targets = []; }
    show(isIntersecting) { for (const target of this.targets) this.callback([{ target, isIntersecting }]); }
  }
  const live = new Set();
  const timers = {
    setTimeout: (fn, ms) => { const id = setTimeout(() => { live.delete(id); fn(); }, ms); live.add(id); return id; },
    clearTimeout: id => { if (id !== null && id !== undefined) { clearTimeout(id); live.delete(id); } },
  };
  return { doc, urls, created, revoked, Observer, observers, timers, live };
}
// Источник кадров: считает запросы и одновременные запросы.
function frameSource({ delayMs = 8, fail = null } = {}) {
  const source = { calls: 0, inFlight: 0, maxInFlight: 0, signals: [] };
  source.fetch = async ({ signal } = {}) => {
    source.calls += 1; source.inFlight += 1; source.maxInFlight = Math.max(source.maxInFlight, source.inFlight);
    source.signals.push(signal);
    try {
      await sleep(delayMs);
      const failure = typeof fail === "function" ? fail(source.calls) : fail;
      if (failure) throw failure;
      return jpeg();
    } finally { source.inFlight -= 1; }
  };
  return source;
}
function statusSource(value = { cameraId: "gate-1", online: true, fps: 7.8, lastFrameAgeMs: 40 }) {
  const source = { calls: 0 };
  source.fetch = async () => { source.calls += 1; return value; };
  return source;
}
function camera(env, frames, status, extra = {}) {
  return createCameraLive({ enabled: true, fetchFrame: frames.fetch, fetchStatus: status.fetch, frameIntervalMs: 1, statusIntervalMs: 20, offlineRetryMs: 30,
    doc: env.doc, urls: env.urls, Observer: env.Observer, timers: env.timers, ...extra });
}

test("камера: кадры идут строго по одному, повторные render() не плодят запросы и таймеры", async () => {
  const env = fakeEnv(), frames = frameSource(), status = statusSource(), page = fakePage();
  const live = camera(env, frames, status);
  try {
    for (let i = 0; i < 20; i++) live.sync(page.root);
    env.doc.fire("visibilitychange"); env.doc.fire("visibilitychange");
    env.observers[0].show(true); env.observers[0].show(true);
    await until(() => frames.calls >= 6, "шесть кадров");
    for (let i = 0; i < 10; i++) { live.sync(page.root); env.doc.fire("visibilitychange"); }
    await until(() => frames.calls >= 10, "десять кадров");
    assert.equal(frames.maxInFlight, 1, "одновременно уходит не больше одного запроса кадра");
    assert.ok(env.live.size <= 2, `запланировано не больше двух таймеров (кадр и статус), есть ${env.live.size}`);
    assert.equal(env.observers.length, 1, "наблюдатель видимости один на блок");
    assert.equal(env.doc.listeners.size, 1, "один обработчик visibilitychange");
    assert.equal(live.snapshot().mode, "live");
    assert.equal(page.parts["#camera-live-img"].getAttribute("src"), live.snapshot().url, "картинка показывает последний кадр");
    assert.equal(page.parts["#camera-live-img"].hidden, false);
    assert.equal(page.parts["#camera-live-note"].hidden, true);
    await until(() => status.calls >= 1, "статус камеры");
    assert.match(page.parts["#camera-live-status-text"].textContent, /^В эфире · 8 к\/с$/);
    assert.match(page.parts["#camera-live-status"].className, /is-online/);
    // Разметка для общего render() совпадает с тем, что модуль уже поставил в DOM.
    const html = live.html();
    assert.ok(html.includes(`src="${live.snapshot().url}"`));
    assert.ok(html.includes("В эфире · 8 к/с"));
    assert.ok(html.includes(CAMERA_TEXT.hint));
  } finally { live.stop(); }
});

test("камера: скрытая вкладка и блок вне экрана — пауза, возврат — опрос продолжается", async () => {
  const env = fakeEnv(), frames = frameSource(), status = statusSource(), page = fakePage();
  const live = camera(env, frames, status);
  try {
    live.sync(page.root);
    await until(() => frames.calls >= 3, "первые кадры");
    env.doc.visibilityState = "hidden"; env.doc.fire("visibilitychange");
    await until(() => frames.inFlight === 0, "начатый запрос завершился");
    await sleep(15);
    const paused = { frames: frames.calls, status: status.calls };
    await sleep(80);
    assert.equal(frames.calls, paused.frames, "на скрытой вкладке кадры не запрашиваются");
    assert.equal(status.calls, paused.status, "на скрытой вкладке статус не запрашивается");
    assert.equal(env.live.size, 0, "таймеров нет");
    assert.equal(live.snapshot().active, false);
    env.doc.visibilityState = "visible"; env.doc.fire("visibilitychange");
    await until(() => frames.calls >= paused.frames + 3, "опрос после возврата");

    env.observers[0].show(false);
    await until(() => frames.inFlight === 0, "запрос завершился");
    await sleep(15);
    const offscreen = frames.calls;
    await sleep(60);
    assert.equal(frames.calls, offscreen, "блок вне экрана — кадры не запрашиваются");
    env.observers[0].show(true);
    await until(() => frames.calls >= offscreen + 2, "опрос после появления блока");
    assert.equal(frames.maxInFlight, 1);
  } finally { live.stop(); }
});

test("камера: прежний blob URL освобождается, остановка снимает таймеры и освобождает последний", async () => {
  const env = fakeEnv(), frames = frameSource(), status = statusSource(), page = fakePage();
  const live = camera(env, frames, status);
  live.sync(page.root);
  await until(() => env.created.length >= 5, "пять кадров");
  assert.deepEqual(env.revoked, env.created.slice(0, -1), "каждый прежний кадр освобождён, в памяти только последний");
  assert.equal(page.parts["#camera-live-img"].getAttribute("src"), env.created.at(-1));
  // Блок исчез из разметки (другой экран, выход) — следующий render() останавливает опрос.
  page.unmount();
  live.sync(page.root);
  assert.deepEqual([...env.revoked].sort(), [...env.created].sort(), "все blob URL освобождены");
  assert.equal(env.live.size, 0, "таймеров не осталось");
  assert.equal(env.observers[0].disconnected, true);
  assert.equal(env.doc.listeners.size, 0, "обработчик visibilitychange снят");
  assert.ok(frames.signals.at(-1).aborted || frames.inFlight === 0, "начатый запрос отменён");
  const stopped = frames.calls;
  await sleep(60);
  assert.equal(frames.calls, stopped, "после остановки кадры не запрашиваются");
  assert.equal(env.created.length, env.revoked.length, "поздний ответ не создаёт новый blob URL");
  assert.equal(live.snapshot().url, null);
});

test("камера: 503 CAMERA_OFFLINE — плашка «Камера офлайн» без кодов и редкий повтор", async () => {
  const env = fakeEnv(), page = fakePage();
  const offline = new ApiError("Камера на входе сейчас не передаёт видео", 503, "CAMERA_OFFLINE");
  const frames = frameSource({ delayMs: 1, fail: call => call === 1 ? null : offline });
  const status = statusSource({ cameraId: "gate-1", online: false, fps: null, lastFrameAgeMs: null });
  const live = camera(env, frames, status, { offlineRetryMs: 1000 });
  try {
    live.sync(page.root);
    await until(() => live.snapshot().mode === "offline", "состояние офлайн");
    assert.equal(live.snapshot().url, null, "старый кадр не выдаётся за живое видео");
    assert.deepEqual(env.revoked, env.created, "кадр до отказа освобождён");
    assert.equal(page.parts["#camera-live-img"].hidden, true);
    assert.equal(page.parts["#camera-live-note"].hidden, false);
    assert.equal(page.parts["#camera-live-note"].textContent, "Камера офлайн — включите трансляцию на iPhone (Larix)");
    assert.match(page.parts["#camera-live-status"].className, /is-offline/);
    const html = live.html();
    assert.ok(html.includes("Камера офлайн — включите трансляцию на iPhone (Larix)"));
    assert.doesNotMatch(html, /503|CAMERA_OFFLINE|<img[^>]*src=/);
    const calls = frames.calls;
    await sleep(120);
    assert.equal(frames.calls, calls, "повтор не раньше, чем через offlineRetryMs");
  } finally { live.stop(); }
});

test("камера: статус снова online — кадр берётся сразу, не дожидаясь редкого повтора", async () => {
  const env = fakeEnv(), page = fakePage();
  let cameraUp = false;
  const frames = frameSource({ delayMs: 1, fail: () => cameraUp ? null : new ApiError("нет видео", 503, "CAMERA_OFFLINE") });
  const status = { calls: 0, fetch: async () => { status.calls += 1; return { cameraId: "gate-1", online: cameraUp, fps: cameraUp ? 8 : null, lastFrameAgeMs: null }; } };
  const live = camera(env, frames, status, { offlineRetryMs: 5000 });
  try {
    live.sync(page.root);
    await until(() => live.snapshot().mode === "offline", "офлайн");
    cameraUp = true;
    await until(() => live.snapshot().mode === "live", "живое видео после статуса online", 1000);
    assert.equal(frames.maxInFlight, 1);
  } finally { live.stop(); }
});

test("камера: в деморежиме заглушка и ни одного запроса", async () => {
  const env = fakeEnv(), page = fakePage();
  const previousFetch = globalThis.fetch;
  let fetches = 0;
  globalThis.fetch = async () => { fetches += 1; throw new Error("в демо сеть не нужна"); };
  const frames = frameSource(), status = statusSource();
  const live = createCameraLive({ enabled: false, fetchFrame: frames.fetch, fetchStatus: status.fetch, doc: env.doc, urls: env.urls, Observer: env.Observer, timers: env.timers });
  try {
    for (let i = 0; i < 5; i++) live.sync(page.root);
    await sleep(40);
    assert.equal(frames.calls + status.calls + fetches, 0);
    assert.equal(env.observers.length, 0);
    assert.equal(env.doc.listeners.size, 0);
    assert.equal(env.live.size, 0);
    const html = live.html();
    assert.ok(html.includes("Камера доступна только на стенде"));
    assert.ok(html.includes("Камера на входе"));
    assert.doesNotMatch(html, /<img/);
  } finally { live.stop(); globalThis.fetch = previousFetch; }
});

test("камера: адаптер API — кадр с токеном оператора как Blob, 503 как ApiError, статус по контракту", async () => {
  const previous = { fetch: globalThis.fetch, mode: config.mode, session: { ...session }, sessionStorage: globalThis.sessionStorage };
  globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  config.mode = "api";
  configureAuthTransport(async path => path === "/auth/demo"
    ? { accessToken: "test-camera-token", refreshToken: "refresh", expiresIn: 300, user: { preferred_username: "demo", roles: ["controller"] } } : {});
  const calls = [];
  let mode = "ok";
  globalThis.fetch = async (url, options) => {
    calls.push({ url, ...options });
    const path = new URL(url).pathname;
    if (path.endsWith("/camera/status")) return Response.json({ camera_id: "gate-1", online: true, fps: 8.4, last_frame_age_ms: 61 });
    if (mode === "offline") return Response.json({ code: "CAMERA_OFFLINE", message: "Камера на входе сейчас не передаёт видео" }, { status: 503 });
    return new Response(jpeg(), { status: 200, headers: { "Content-Type": "image/jpeg", "Cache-Control": "no-store" } });
  };
  try {
    await demoLogin("controller");
    const frame = await backendApi.cameraFrame({ signal: new AbortController().signal });
    assert.ok(frame instanceof Blob);
    assert.equal(frame.type, "image/jpeg");
    assert.equal(frame.size, 4);
    const call = calls.at(-1);
    assert.equal(new URL(call.url).pathname, "/api/camera/frame.jpg");
    assert.equal(new URL(call.url).search, "", "токен не кладётся в адрес");
    assert.equal(call.method, "GET");
    assert.equal(call.headers.Authorization, "Bearer test-camera-token");
    assert.equal(call.cache, "no-store");

    mode = "offline";
    await assert.rejects(backendApi.cameraFrame(), error => error instanceof ApiError && error.status === 503 && error.code === "CAMERA_OFFLINE" &&
      error.message === "Камера на входе сейчас не передаёт видео");

    assert.deepEqual(await backendApi.cameraStatus(), { cameraId: "gate-1", online: true, fps: 8.4, lastFrameAgeMs: 61 });
    assert.equal(calls.at(-1).headers.Authorization, "Bearer test-camera-token");
    assert.equal(calls.at(-1).cache, undefined, "JSON-запросы кэш не трогают");
  } finally {
    await logout().catch(() => {});
    configureAuthTransport(request);
    globalThis.fetch = previous.fetch;
    globalThis.sessionStorage = previous.sessionStorage;
    config.mode = previous.mode;
    Object.assign(session, previous.session);
  }
});

test("«Как проверить кейс»: в шаге контролёра упомянуто живое видео камеры", async () => {
  const { GUIDE_STEPS } = await import("../src/landing.js");
  const step = GUIDE_STEPS.find(item => item.role === "controller" && item.title === "Контролёр пропускает человека");
  assert.ok(step.text.includes("Сверху — живое видео камеры на входе с рамками"));
});
