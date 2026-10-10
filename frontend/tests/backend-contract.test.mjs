import test from "node:test";
import assert from "node:assert/strict";
import { ApiError, request, backendApi, mapPerson, mapEvent, appearancesFromRoutes } from "../src/api.js";
import { CASE_ZONES, caseZoneMetrics } from "../src/zones.js";
import { session, initAuth, logout } from "../src/auth.js";
import { config } from "../config.js";

test("backend adapter sends person multipart and passage direction", async () => {
  const previousFetch = globalThis.fetch, previousLocation = globalThis.location;
  globalThis.location = new URL("http://localhost:8080/?devRole=admin");
  session.dev = true;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, ...options });
    const path = new URL(url).pathname;
    const value = path.includes("/persons") ? { id: 1, fio: "Иванов Иван", position: "Монтажник", group: { id: 2, name: "Звено" }, status: "active", on_site: false, has_face: true, has_active_device: false } :
      path.endsWith("/passages") ? { session_id: "e0ae024b-c511-47d6-903a-f2099d94fa03", status: "pending", direction: "in", expires_at: "2026-10-10T12:00:00Z",
        person: { id: 1, fio: "Иванов Иван" }, decision: null, reason_code: null, reason: null, code_delivered: false } :
      { decision: "allow", person: { id: 1, fio: "Иванов Иван" }, shift: null };
    return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  try {
    const form = new FormData();
    form.set("name", "Иванов Иван"); form.set("role", "Монтажник"); form.set("groupId", "2");
    form.set("referencePhoto", new Blob(["jpg"], { type: "image/jpeg" }), "face.jpg");
    const person = await backendApi.saveEmployee(null, form);
    assert.equal(person.employee.groupId, 2);
    assert.equal(calls[0].method, "POST");
    assert.equal(calls[0].body.get("fio"), "Иванов Иван");
    assert.equal(calls[0].body.get("group_id"), "2");
    assert.equal(calls[0].body.get("photo").name, "face.jpg");
    const edit = new FormData(); edit.set("name", "Иванов Иван"); edit.set("role", "Монтажник"); edit.set("groupId", "");
    await backendApi.saveEmployee("1", edit);
    assert.equal(calls[1].method, "PATCH");
    assert.equal(calls[1].body.get("group_id"), "");
    const challenge = await backendApi.createChallenge("entry", "1");
    assert.deepEqual(JSON.parse(calls[2].body), { person_id: 1, direction: "in" });
    assert.equal(challenge.status, "waiting");
    assert.equal(challenge.personName, "Иванов Иван");
    assert.equal(challenge.code, "");
  } finally {
    globalThis.fetch = previousFetch;
    globalThis.location = previousLocation;
    session.dev = false;
  }
});

test("face upload accepts 1–10 frames, sends controller token and preserves 401/403 failures", async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location, history: globalThis.history,
    sessionStorage: globalThis.sessionStorage, mode: config.mode, session: { ...session } };
  const state = "test-login-state";
  globalThis.location = new URL(`http://localhost:8080/?code=test-code&state=${state}`);
  globalThis.history = { replaceState() {} };
  globalThis.sessionStorage = {
    getItem: () => JSON.stringify({ state, verifier: "test-verifier", createdAt: Date.now(), redirectUri: "http://localhost:8080/" }),
    removeItem() {},
  };
  config.mode = "api";
  session.dev = false;
  const uploads = [];
  let status = 200;
  globalThis.fetch = async (url, options) => {
    if (url.endsWith("/auth/exchange")) return Response.json({ accessToken: "test-controller-token", expiresIn: 300,
      user: { preferred_username: "Контролёр", roles: ["controller"] } });
    uploads.push({ url, ...options });
    return Response.json(status === 200 ? { decision: "allow", person: { fio: "Иванов Иван" } } : { code: `HTTP_${status}`, message: "Ошибка авторизации" }, { status });
  };
  const frame = new Blob(["jpg"], { type: "image/jpeg" });
  try {
    await initAuth();
    assert.deepEqual(session.roles, ["controller"]);
    for (const count of [1, 3, 10]) {
      const result = await backendApi.decidePass("session-id", "entry", Array(count).fill(frame));
      const upload = uploads.at(-1);
      assert.equal(result.outcome, "allowed");
      assert.ok(upload.url.endsWith("/passages/session-id/face"));
      assert.equal(upload.method, "POST");
      assert.equal(upload.body.getAll("frames").length, count);
      assert.equal(upload.headers.Authorization, "Bearer test-controller-token");
      assert.equal(upload.headers["Content-Type"], undefined); // fetch supplies the multipart boundary.
    }
    for (const invalid of [[], Array(11).fill(frame), [new Blob([])], ["not a frame"], frame]) {
      await assert.rejects(backendApi.decidePass("session-id", "entry", invalid), /от 1 до 10/);
    }
    assert.equal(uploads.length, 3);
    for (const code of [401, 403]) {
      status = code;
      await assert.rejects(backendApi.decidePass("session-id", "entry", [frame]), error =>
        error instanceof ApiError && error.status === code && error.message === "Ошибка авторизации");
    }
    await logout();
    const before = uploads.length;
    await assert.rejects(backendApi.decidePass("session-id", "entry", [frame]), /Сессия истекла/);
    assert.equal(uploads.length, before);
  } finally {
    await logout();
    config.mode = previous.mode;
    Object.assign(session, previous.session);
    for (const key of ["fetch", "location", "history", "sessionStorage"]) globalThis[key] = previous[key];
  }
});

test("phone receives technical refusal messages without expiring an operator session", async () => {
  const previousFetch = globalThis.fetch, previousWindow = globalThis.window;
  const target = new EventTarget();
  let expired = 0;
  target.addEventListener("operator-session-expired", () => expired++);
  globalThis.window = target;
  const failures = [
    [404, "SESSION_NOT_FOUND", "Попытка прохода не найдена"],
    [403, "DEVICE_INVALID", "Телефон не подтверждён, обратитесь к администратору"],
    [409, "SESSION_EXPIRED", "Срок попытки прохода истёк"],
    [403, "SESSION_FOREIGN", "Эта попытка принадлежит другому работнику"],
  ];
  try {
    for (const [status, code, message] of failures) {
      globalThis.fetch = async () => Response.json({ code, message }, { status });
      await assert.rejects(request("/passages/session-id/sign", { method: "POST", auth: "none", body: {} }),
        error => error instanceof ApiError && error.status === status && error.code === code && error.message === message);
    }
    for (const [status, message] of [[401, "Сессия истекла, войдите заново"], [403, "Недостаточно прав"]]) {
      globalThis.fetch = async () => Response.json({}, { status });
      await assert.rejects(request("/passages/session-id/sign", { auth: "none" }), error => error.message === message);
    }
    assert.equal(expired, 0);
  } finally {
    globalThis.fetch = previousFetch;
    globalThis.window = previousWindow;
  }
});

test("200 deny is a business result and face requests wait past the 30 second proxy timeout", async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location, session: { ...session }, timeout: AbortSignal.timeout };
  globalThis.location = new URL("http://localhost:8080/?devRole=controller");
  session.dev = true;
  const timeouts = [];
  AbortSignal.timeout = milliseconds => { timeouts.push(milliseconds); return previous.timeout(milliseconds); };
  globalThis.fetch = async () => Response.json({ decision: "deny", reason_code: "FACE_MISMATCH", reason: "Лицо не совпадает с эталоном", person: { fio: "Иванов Иван" } });
  try {
    const result = await backendApi.decidePass("session-id", "exit", [new Blob(["jpg"])]);
    assert.equal(result.outcome, "denied");
    assert.equal(result.reason, "Лицо не совпадает с эталоном");
    assert.equal(result.reasonCode, "FACE_MISMATCH");
    assert.equal(result.direction, "exit");
    assert.equal(timeouts.at(-1), 35000);
    await request("/health", { auth: "none" });
    assert.equal(timeouts.at(-1), 15000);
  } finally {
    AbortSignal.timeout = previous.timeout;
    globalThis.fetch = previous.fetch;
    globalThis.location = previous.location;
    Object.assign(session, previous.session);
  }
});

test("photo rejection keeps FACE code and human message on create and update", async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location, session: { ...session } };
  globalThis.location = new URL("http://localhost:8080/?devRole=admin");
  session.dev = true;
  const failures = [
    { status: 422, code: "FACE_NO_FACE", message: "Лицо не найдено. Выберите другое фото." },
    { status: 422, code: "FACE_MULTIPLE_FACES", message: "На фото несколько лиц." },
    { status: 503, code: "FACE_SERVICE_UNAVAILABLE", message: "Сервис распознавания недоступен, попробуйте позже" },
  ];
  try {
    const form = new FormData();
    form.set("name", "Иванов Иван");
    form.set("referencePhoto", new Blob(["jpg"], { type: "image/jpeg" }), "face.jpg");
    for (const failure of failures) {
      globalThis.fetch = async () => Response.json({ code: failure.code, message: failure.message }, { status: failure.status });
      for (const id of [null, "1"]) {
        await assert.rejects(backendApi.saveEmployee(id, form), error => error instanceof ApiError &&
          error.status === failure.status && error.code === failure.code && error.message === failure.message);
      }
    }
  } finally {
    globalThis.fetch = previous.fetch;
    globalThis.location = previous.location;
    Object.assign(session, previous.session);
  }
});

test("backend field names map to displayed status and journal", () => {
  assert.equal(mapPerson({ id: 3, fio: "Петров", status: "blocked", on_site: false }).blocked, true);
  assert.deepEqual(mapEvent({ id: 4, ts: "2026-10-10T00:00:00Z", direction: "out", result: "deny", reason: "Отказ" }).outcome, "denied");
});

test("face refusal objects display human text in decision and journal", async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location, session: { ...session } };
  globalThis.location = new URL("http://localhost:8080/?devRole=controller");
  session.dev = true;
  try {
    for (const [code, text] of [["FACE_OCCLUDED", "Лицо закрыто"], ["FACE_MULTIPLE_FACES", "В кадре несколько лиц. Проходите по одному"]]) {
      const value = { decision: "deny", result: "deny", reason: { code, text } };
      globalThis.fetch = async () => Response.json(value);
      const result = await backendApi.decidePass("id", "entry", [new Blob(["jpg"])]);
      assert.equal(result.reason, text);
      assert.equal(result.reasonCode, code);
      assert.equal(mapEvent(value).reason, text);
    }
  } finally {
    globalThis.fetch = previous.fetch;
    globalThis.location = previous.location;
    Object.assign(session, previous.session);
  }
});

test("sightings list uses route timestamps, worker names and configured zone names", () => {
  const routes = [
    { employeeId: "1", employeeName: "Иванов Иван", points: [{ at: "2026-10-10T07:00:00Z", zoneId: 2 }] },
    { employeeId: "2", employeeName: "Петров Пётр", points: [{ at: "2026-10-10T08:00:00Z", zoneId: null }] },
  ];
  const entries = appearancesFromRoutes(routes, [{ id: "2", name: "Место для курения" }]);
  assert.deepEqual(entries.map(({ employeeName, zoneName }) => [employeeName, zoneName]),
    [["Петров Пётр", "Вне трёх заданных зон"], ["Иванов Иван", "Курилка"]]);
  assert.equal(entries[1].at, "2026-10-10T07:00:00Z");
  assert.deepEqual(caseZoneMetrics([{ id: "9", name: "Склад", count: 120 }, { id: "2", name: "Место для курения", count: 4 }]).map(zone => zone.name), CASE_ZONES);
  assert.equal(caseZoneMetrics([{ id: "9", name: "Склад", count: 120 }])[0].available, false);
});

const SESSION_ID = "e0ae024b-c511-47d6-903a-f2099d94fa03";
const sessionBody = patch => ({ session_id: SESSION_ID, status: "pending", direction: "in", expires_at: "2026-10-10T12:00:00Z",
  person: { id: 7, fio: "Иванов Иван" }, decision: null, reason_code: null, reason: null, code_delivered: false, ...patch });

test("controller finds people, enters the six-digit code and sees refusals as results", async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location, session: { ...session } };
  globalThis.location = new URL("http://localhost:8080/?devRole=controller");
  session.dev = true;
  const calls = [];
  let reply;
  globalThis.fetch = async (url, options) => { calls.push({ url: new URL(url), ...options }); return Response.json(reply); };
  try {
    reply = [{ id: 7, fio: "Иванов Иван", position: "Монтажник", group: { id: 2, name: "Звено" }, status: "active", on_site: false, has_face: true, has_active_device: true }];
    const people = await backendApi.gatePeople("Иван");
    assert.equal(calls[0].url.pathname, "/api/passages/people");
    assert.equal(calls[0].url.searchParams.get("q"), "Иван");
    assert.equal(calls[0].url.searchParams.get("limit"), "50");
    assert.deepEqual(people.map(person => [person.id, person.name, person.enrolled]), [["7", "Иванов Иван", true]]);

    reply = sessionBody({ status: "signed" });
    const confirmed = await backendApi.verifyCode(SESSION_ID, "123456");
    assert.equal(calls[1].url.pathname, `/api/passages/${SESSION_ID}/code`);
    assert.equal(calls[1].method, "POST");
    assert.deepEqual(JSON.parse(calls[1].body), { code: "123456" });
    assert.equal(confirmed.status, "confirmed");

    reply = sessionBody({ reason_code: "CODE_INVALID", reason: "Код не совпадает. Осталось попыток: 2" });
    await assert.rejects(backendApi.verifyCode(SESSION_ID, "000000"),
      error => error instanceof ApiError && error.code === "CODE_INVALID" && /Осталось попыток: 2/.test(error.message));

    reply = sessionBody({ status: "decided", decision: "deny", reason_code: "NO_ACTIVE_DEVICE", reason: "У человека нет активированного телефона, обратитесь к администратору" });
    const refused = await backendApi.createChallenge("entry", "7");
    assert.equal(refused.status, "used");
    assert.equal(refused.decision, "deny");
    assert.equal(refused.reasonCode, "NO_ACTIVE_DEVICE");
    assert.match(refused.reason, /нет активированного телефона/);
  } finally {
    globalThis.fetch = previous.fetch;
    globalThis.location = previous.location;
    Object.assign(session, previous.session);
  }
});

function fakeIndexedDb(initial) {
  const data = new Map(Object.entries(initial));
  return { open() {
    const opened = { result: { createObjectStore() {}, close() {}, transaction() {
      const tx = {};
      tx.objectStore = () => ({
        get: key => { const req = { result: data.get(key) }; setTimeout(() => tx.oncomplete?.(), 0); return req; },
        put: (value, key) => { data.set(key, value); const req = {}; setTimeout(() => tx.oncomplete?.(), 0); return req; },
        delete: key => { data.delete(key); const req = {}; setTimeout(() => tx.oncomplete?.(), 0); return req; },
      });
      return tx;
    } } };
    setTimeout(() => { opened.onupgradeneeded?.(); opened.onsuccess?.(); }, 0);
    return opened;
  } };
}

test("phone signs pass-code|device_id|ts with the device key and shows the code or no pending pass", async () => {
  const previous = { fetch: globalThis.fetch, indexedDB: globalThis.indexedDB };
  const keys = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
  const deviceId = "7f1d2c9e-5a64-4b0e-9c1b-0d9e8a7b6c5d";
  globalThis.indexedDB = fakeIndexedDb({ current: { privateKey: keys.privateKey, publicKey: {}, deviceId, person: { id: 7, fio: "Иванов Иван" } } });
  const calls = [];
  let reply = { pending: true, session_id: SESSION_ID, direction: "out", code: "042917", expires_at: "2099-10-10T12:00:00Z" };
  globalThis.fetch = async (url, options) => { calls.push({ url: new URL(url), ...options }); return Response.json(reply); };
  try {
    const passage = await backendApi.workerPassCode();
    assert.equal(calls[0].url.pathname, "/api/device/pass-code");
    assert.equal(calls[0].method, "POST");
    assert.equal(calls[0].headers.Authorization, undefined);
    const body = JSON.parse(calls[0].body);
    assert.deepEqual(Object.keys(body).sort(), ["device_id", "signature", "ts"]);
    assert.equal(body.device_id, deviceId);
    assert.equal(Number.isNaN(Date.parse(body.ts)), false);
    assert.match(body.ts, /Z$|[+-]\d\d:\d\d$/);
    const signature = Uint8Array.from(atob(body.signature.replace(/-/g, "+").replace(/_/g, "/")), char => char.charCodeAt(0));
    assert.equal(signature.length, 64);
    const verify = text => crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, keys.publicKey, signature, new TextEncoder().encode(text));
    assert.equal(await verify(`pass-code|${deviceId}|${body.ts}`), true);
    assert.equal(await verify(`pass-code|${deviceId}|${body.ts}x`), false);
    assert.deepEqual(passage, { id: SESSION_ID, direction: "exit", code: "042917", expiresAt: "2099-10-10T12:00:00Z", personName: "Иванов Иван" });

    reply = { pending: false };
    assert.equal(await backendApi.workerPassCode(), null);
  } finally {
    globalThis.fetch = previous.fetch;
    globalThis.indexedDB = previous.indexedDB;
  }
});
