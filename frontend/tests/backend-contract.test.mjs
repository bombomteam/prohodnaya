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
      path.endsWith("/passages") ? { session_id: "e0ae024b-c511-47d6-903a-f2099d94fa03", nonce: "n", direction: "in", expires_at: "2026-10-10T12:00:00Z", qr_payload: '{"sid":"e0ae024b-c511-47d6-903a-f2099d94fa03","n":"n","d":"in"}' } :
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
    const challenge = await backendApi.createChallenge("entry");
    assert.deepEqual(JSON.parse(calls[2].body), { direction: "in" });
    assert.match(challenge.code, /"sid"/);
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
