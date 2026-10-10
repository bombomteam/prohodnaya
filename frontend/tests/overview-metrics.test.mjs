import test from "node:test";
import assert from "node:assert/strict";
import { backendApi, resolveApiUrl, startOfDayIso } from "../src/api.js";
import { session } from "../src/auth.js";
import { config } from "../config.js";

test("relative file addresses from the backend are resolved against the API address", () => {
  assert.equal(resolveApiUrl("/api/files/plan/plan.png", "https://stand.example/api", "https://stand.example/"), "https://stand.example/api/files/plan/plan.png");
  assert.equal(resolveApiUrl("/api/files/plan/plan.png", "/api", "https://stand.example/app/"), "https://stand.example/api/files/plan/plan.png");
  assert.equal(resolveApiUrl("/api/files/plan/plan.png", "http://localhost:8000/api", undefined), "http://localhost:8000/api/files/plan/plan.png");
  assert.equal(resolveApiUrl("https://cdn.example/plan.png", "/api", "https://stand.example/"), "https://cdn.example/plan.png");
  for (const unsafe of ["javascript:alert(1)", "data:image/png;base64,AAAA", "", "   ", null, undefined, 42]) {
    assert.equal(resolveApiUrl(unsafe, "https://stand.example/api", "https://stand.example/"), null, String(unsafe));
  }
  assert.equal(resolveApiUrl("/api/files/plan/plan.png", "/api", undefined), null);
});

test("start of the day follows the site time zone, not the browser one", () => {
  assert.equal(startOfDayIso(new Date("2026-10-10T21:30:00Z"), "Europe/Moscow"), "2026-10-10T21:00:00.000Z");
  assert.equal(startOfDayIso(new Date("2026-10-10T10:00:00Z"), "Europe/Moscow"), "2026-10-09T21:00:00.000Z");
  assert.equal(startOfDayIso(new Date("2026-10-10T10:00:00Z"), "UTC"), "2026-10-10T00:00:00.000Z");
});

function withBackend(roles, handler, body) {
  const previous = { fetch: globalThis.fetch, location: globalThis.location, roles: session.roles, dev: session.dev, kind: session.kind };
  globalThis.location = new URL("http://localhost:8080/");
  Object.assign(session, { kind: "operator", roles, dev: true });
  const calls = [];
  globalThis.fetch = async url => {
    const target = new URL(url);
    calls.push(target);
    return Response.json(handler(target));
  };
  return body(calls).finally(() => {
    globalThis.fetch = previous.fetch; globalThis.location = previous.location;
    Object.assign(session, { kind: previous.kind, roles: previous.roles, dev: previous.dev });
  });
}

const person = id => ({ id, fio: `Сотрудник ${id}`, position: "", group: null, status: "active", on_site: id === 1, has_face: true, has_active_device: id !== 3 });
const event = (id, result) => ({ id, ts: "2026-10-10T08:00:00Z", person: { id: 1, fio: "Сотрудник 1" }, direction: "in", result, reason_code: null, reason: result === "deny" ? "Лицо не совпадает" : null });
const answer = target => {
  if (target.pathname.endsWith("/site/now")) return [{ person_id: 1, fio: "Сотрудник 1", group: null, since: "2026-10-10T07:00:00Z", last_seen_at: null, last_zone: null }];
  if (target.pathname.endsWith("/persons")) return [person(1), person(2), person(3)];
  if (target.pathname.endsWith("/events")) return target.searchParams.has("from") ? [event(3, "deny"), event(2, "allow"), event(1, "deny")] : [event(3, "deny")];
  throw new Error(`unexpected ${target.pathname}`);
};

test("admin overview counts employees, passes and denials from the available routes", async () => {
  await withBackend(["admin"], answer, async calls => {
    const dashboard = await backendApi.dashboard();
    assert.equal(dashboard.inside.length, 1);
    assert.equal(dashboard.totalEmployees, 3);
    assert.equal(dashboard.deniedToday, 2);
    assert.equal(dashboard.allowedToday, 1);
    assert.equal(dashboard.todayCapped, false);
    const todayCall = calls.find(call => call.searchParams.has("from"));
    assert.equal(todayCall.searchParams.get("limit"), "1000");
    assert.equal(todayCall.searchParams.get("from"), startOfDayIso());
    assert.ok(calls.some(call => call.pathname.endsWith("/persons")));
  });
});

test("manager overview does not ask for the admin-only employee list and keeps the rest", async () => {
  await withBackend(["manager"], answer, async calls => {
    const dashboard = await backendApi.dashboard();
    assert.equal(dashboard.totalEmployees, null);
    assert.equal(dashboard.deniedToday, 2);
    assert.ok(!calls.some(call => call.pathname.endsWith("/persons")));
  });
});

test("a failing aggregate leaves the metric empty instead of failing the whole overview", async () => {
  await withBackend(["admin"], target => {
    if (target.pathname.endsWith("/events") && target.searchParams.has("from")) throw new Error("boom");
    return answer(target);
  }, async () => {
    const dashboard = await backendApi.dashboard();
    assert.equal(dashboard.deniedToday, null);
    assert.equal(dashboard.allowedToday, null);
    assert.equal(dashboard.totalEmployees, 3);
  });
});

test("site plan with a relative image address gets a displayable picture URL", async () => {
  await withBackend(["manager"], () => ({ image_url: "/api/files/plan/plan.png", width: 800, height: 600, calibration: null, zones: [] }), async () => {
    const site = await backendApi.site();
    assert.equal(site.planImageUrl, new URL("/api/files/plan/plan.png", config.apiBaseUrl).href);
    assert.equal(site.rawImageUrl, "/api/files/plan/plan.png");
  });
});
