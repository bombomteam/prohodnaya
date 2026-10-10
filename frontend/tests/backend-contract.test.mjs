import test from "node:test";
import assert from "node:assert/strict";
import { backendApi, mapPerson, mapEvent, appearancesFromRoutes } from "../src/api.js";
import { session } from "../src/auth.js";

test("backend adapter sends person multipart, passage direction and three frames", async () => {
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
    const frames = Array.from({ length: 3 }, () => new Blob(["jpg"], { type: "image/jpeg" }));
    const decision = await backendApi.decidePass(challenge.id, "entry", frames);
    assert.equal(calls[3].body.getAll("frames").length, 3);
    assert.equal(decision.outcome, "allowed");
    assert.equal(calls[3].headers.Authorization, undefined);
  } finally {
    globalThis.fetch = previousFetch;
    globalThis.location = previousLocation;
    session.dev = false;
  }
});

test("backend field names map to displayed status and journal", () => {
  assert.equal(mapPerson({ id: 3, fio: "Петров", status: "blocked", on_site: false }).blocked, true);
  assert.deepEqual(mapEvent({ id: 4, ts: "2026-10-10T00:00:00Z", direction: "out", result: "deny", reason: "Отказ" }).outcome, "denied");
});

test("sightings list uses route timestamps, worker names and configured zone names", () => {
  const routes = [
    { employeeId: "1", employeeName: "Иванов Иван", points: [{ at: "2026-10-10T07:00:00Z", zoneId: 2 }] },
    { employeeId: "2", employeeName: "Петров Пётр", points: [{ at: "2026-10-10T08:00:00Z", zoneId: null }] },
  ];
  const entries = appearancesFromRoutes(routes, [{ id: "2", name: "Склад" }]);
  assert.deepEqual(entries.map(({ employeeName, zoneName }) => [employeeName, zoneName]),
    [["Петров Пётр", "Вне размеченных зон"], ["Иванов Иван", "Склад"]]);
  assert.equal(entries[1].at, "2026-10-10T07:00:00Z");
});
