import test from "node:test";
import assert from "node:assert/strict";
import { demoApi } from "../src/demo-api.js";
import { appearancesFromRoutes } from "../src/api.js";
import { CASE_ZONES } from "../src/zones.js";

const storage = new Map();
globalThis.localStorage = {
  getItem: (key) => storage.get(key) ?? null,
  setItem: (key, value) => storage.set(key, value),
};

test("demo walkthrough links phone, gate, shift and report", async () => {
  const form = new FormData();
  form.set("name", "Тестовый Рабочий");
  form.set("company", "Подрядчик");
  form.set("role", "Монтажник");
  form.set("referencePhoto", new File(["photo"], "face.jpg", { type: "image/jpeg" }));
  const { employee } = await demoApi.saveEmployee(null, form);
  await demoApi.enroll(employee.id);

  const entry = await demoApi.createChallenge("entry", employee.id);
  assert.equal(entry.code, undefined); // Controller must receive the code from the worker.
  assert.equal(await demoApi.workerPassage("e1"), null);
  assert.equal((await demoApi.workerPassage(employee.id)).code, null);
  await assert.rejects(demoApi.confirmPass(entry.id, { demoEmployeeId: "e1" }), /другому работнику/);
  await assert.rejects(demoApi.verifyCode(entry.id, "000000"), /ещё не подтвердил/);
  await demoApi.confirmPass(entry.id, { demoEmployeeId: employee.id });
  const code = (await demoApi.workerPassage(employee.id)).code;
  assert.match(code, /^\d{6}$/);
  await assert.rejects(demoApi.verifyCode(entry.id, "000000"), /Неверный код/);
  await assert.rejects(demoApi.decidePass(entry.id, "entry", new Blob(["face"])), /подтверждение/);
  await demoApi.verifyCode(entry.id, code);
  await assert.rejects(demoApi.decidePass(entry.id, "entry", new Blob([])), /Снимок/);
  assert.equal((await demoApi.employees()).find(x => x.id === employee.id).status, "outside");
  const allowed = await demoApi.decidePass(entry.id, "entry", new Blob(["face"]));
  assert.equal(allowed.outcome, "allowed");
  assert.equal(allowed.employee.status, "inside");
  await assert.rejects(demoApi.decidePass(entry.id, "entry", new Blob(["face"])), /подтверждение/i);
  await assert.rejects(demoApi.verifyCode(entry.id, code), /недоступен/);
  assert.equal(await demoApi.workerPassage(employee.id), null);

  const site = await demoApi.site();
  assert.deepEqual(site.zones, CASE_ZONES);
  await demoApi.sendLocation(allowed.employee.shiftId, {
    latitude: 55,
    longitude: 37,
    accuracy: 10,
    capturedAt: new Date().toISOString(),
  });
  const day = new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Moscow", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
  const filters = { from: day, to: day, hourFrom: "0", hourTo: "23", employeeIds: employee.id };
  const analytics = await demoApi.analytics(filters);
  assert.equal(analytics.routes.length, 1);
  assert.deepEqual(analytics.zones.map(zone => zone.name), CASE_ZONES);
  assert.equal(analytics.zones.reduce((sum, zone) => sum + zone.count, 0), 0);
  const sightings = appearancesFromRoutes(analytics.routes, analytics.zones);
  assert.equal(sightings.length, 1);
  assert.equal(sightings[0].employeeName, "Тестовый Рабочий");
  assert.equal(sightings[0].zoneName, "Вне трёх заданных зон");

  const exit = await demoApi.createChallenge("exit", employee.id);
  await demoApi.confirmPass(exit.id, { demoEmployeeId: employee.id });
  await demoApi.verifyCode(exit.id, (await demoApi.workerPassage(employee.id)).code);
  const finished = await demoApi.decidePass(exit.id, "exit", new Blob(["face"]));
  assert.equal(finished.outcome, "allowed");
  assert.equal(finished.employee.status, "outside");
  await assert.rejects(demoApi.sendLocation(allowed.employee.shiftId, { latitude: 55, longitude: 37, capturedAt: new Date().toISOString() }), /закрыта/i);

  const report = await demoApi.shiftReport(filters);
  assert.equal(report.people.length, 1);
  assert.equal(report.shifts.length, 1);
});

test("demo codes expire and a new request invalidates the previous one", async () => {
  await assert.rejects(demoApi.createChallenge("entry", "missing"), /Выберите сотрудника/);
  await assert.rejects(demoApi.createChallenge("entry", "e4"), /Телефон не подтверждён/);
  const previous = await demoApi.createChallenge("entry", "e3");
  await demoApi.confirmPass(previous.id, { demoEmployeeId: "e3" });
  const oldCode = (await demoApi.workerPassage("e3")).code;
  const next = await demoApi.createChallenge("entry", "e3");
  await assert.rejects(demoApi.verifyCode(previous.id, oldCode), /недоступен/);
  assert.equal((await demoApi.workerPassage("e3")).id, next.id);
  const [key, raw] = [...storage.entries()].find(([, value]) => JSON.parse(value).challenges?.some(x => x.id === next.id));
  const saved = JSON.parse(raw);
  saved.challenges.find(x => x.id === next.id).expiresAt = "2000-01-01T00:00:00Z";
  storage.set(key, JSON.stringify(saved));
  assert.equal((await demoApi.challenge(next.id)).status, "expired");
  assert.equal(await demoApi.workerPassage("e3"), null);
  await assert.rejects(demoApi.confirmPass(next.id, { demoEmployeeId: "e3" }), /истекло/);
  await assert.rejects(demoApi.verifyCode(next.id, oldCode), /истекло/);
});
