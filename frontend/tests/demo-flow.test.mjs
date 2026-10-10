import test from "node:test";
import assert from "node:assert/strict";
import { demoApi } from "../src/demo-api.js";

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

  const entry = await demoApi.createChallenge("entry");
  const entryOptions = await demoApi.passOptions(entry.code);
  assert.equal(entryOptions.challengeId, entry.id);
  await demoApi.confirmPass(entry.id, { demoEmployeeId: employee.id });
  const allowed = await demoApi.decidePass(entry.id, "entry", new Blob(["face"]));
  assert.equal(allowed.outcome, "allowed");
  assert.equal(allowed.employee.status, "inside");
  await assert.rejects(demoApi.decidePass(entry.id, "entry", new Blob(["face"])), /подтверждение/i);

  const site = await demoApi.site();
  await demoApi.sendLocation(allowed.employee.shiftId, {
    latitude: (site.latMin + site.latMax) / 2,
    longitude: (site.lonMin + site.lonMax) / 2,
    accuracy: 10,
    capturedAt: new Date().toISOString(),
  });
  const day = new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Moscow", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
  const filters = { from: day, to: day, hourFrom: "0", hourTo: "23", employeeIds: employee.id };
  const analytics = await demoApi.analytics(filters);
  assert.equal(analytics.routes.length, 1);
  assert.equal(analytics.zones.reduce((sum, zone) => sum + zone.count, 0), 1);

  const exit = await demoApi.createChallenge("exit");
  await demoApi.passOptions(exit.code);
  await demoApi.confirmPass(exit.id, { demoEmployeeId: employee.id });
  const finished = await demoApi.decidePass(exit.id, "exit", new Blob(["face"]));
  assert.equal(finished.outcome, "allowed");
  assert.equal(finished.employee.status, "outside");
  await assert.rejects(demoApi.sendLocation(allowed.employee.shiftId, { latitude: site.latMin, longitude: site.lonMin, capturedAt: new Date().toISOString() }), /закрыта/i);

  const report = await demoApi.shiftReport(filters);
  assert.equal(report.people.length, 1);
  assert.equal(report.shifts.length, 1);
});
