import { config } from "../config.js";
import { accessToken, configureAuthTransport } from "./auth.js";
import { loadDevice, saveDevice, createDevice, parsePassagePayload, signPassage, signLocations } from "./device.js";
import { demoApi } from "./demo-api.js";

export class ApiError extends Error {
  constructor(message, status, code) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export async function request(path, { method = "GET", body, signal, auth = "operator", headers = {} } = {}) {
  if (auth === "operator") {
    const token = await accessToken();
    if (token) headers = { ...headers, Authorization: `Bearer ${token}` };
  }
  const timeout = AbortSignal.timeout(config.requestTimeoutMs || 15000);
  const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;
  const isForm = body instanceof FormData;
  let response;
  try {
    response = await fetch(`${config.apiBaseUrl}${path}`, {
      method,
      credentials: "omit",
      headers: { ...(isForm || body === undefined ? {} : { "Content-Type": "application/json" }), ...headers },
      body: isForm ? body : body === undefined ? undefined : JSON.stringify(body),
      signal: combined,
    });
  } catch (error) {
    if (error.name === "AbortError" || error.name === "TimeoutError") throw new ApiError("Время ожидания истекло. Проверьте результат попытки перед повтором.");
    throw new ApiError("Нет связи с сервером. Проверьте подключение и повторите попытку.");
  }
  let data;
  try { data = await response.json(); } catch { data = null; }
  if (!response.ok) {
    if (response.status === 401 && auth === "operator" && typeof window !== "undefined") window.dispatchEvent(new Event("operator-session-expired"));
    const message = response.status === 401 ? "Сессия истекла, войдите заново" : response.status === 403 ? "Недостаточно прав" :
      (typeof data?.message === "string" ? data.message : typeof data?.detail === "string" ? data.detail : "Не удалось выполнить действие. Повторите попытку.");
    throw new ApiError(message, response.status, data?.code);
  }
  return data;
}

const directionToBackend = direction => direction === "entry" ? "in" : "out";
const directionToUi = direction => direction === "in" ? "entry" : "exit";

export function mapPerson(person) {
  return {
    id: String(person.id), name: person.fio, company: person.group?.name || "", groupId: person.group?.id ?? null,
    role: person.position || "", status: person.on_site ? "inside" : "outside", blocked: person.status === "blocked",
    enrolled: Boolean(person.has_active_device), hasFace: Boolean(person.has_face),
    enteredAt: person.on_site_since || null, lastSeenAt: person.last_seen_at || null, lastZone: person.last_zone?.name || null,
  };
}

export function mapEvent(event) {
  return {
    id: String(event.id), at: event.ts, employeeId: event.person?.id == null ? null : String(event.person.id),
    employeeName: event.person?.fio || null, direction: event.direction ? directionToUi(event.direction) : null,
    outcome: event.result === "allow" ? "allowed" : "denied", reason: event.reason || (event.result === "allow" ? "Разрешено сервером" : "Отказ сервера"),
    reasonCode: event.reason_code || null,
  };
}

function mapSitePerson(person) {
  return { id: String(person.person_id), name: person.fio, company: person.group?.name || "", role: "", status: "inside",
    enrolled: true, enteredAt: person.since, lastSeenAt: person.last_seen_at, lastZone: person.last_zone?.name || null };
}

function queryFilters(filters) {
  const params = new URLSearchParams({ from: filters.from, to: filters.to });
  if (filters.hourFrom !== "") params.set("hour_from", filters.hourFrom);
  if (filters.hourTo !== "") params.set("hour_to", filters.hourTo);
  if (filters.employeeIds) params.set("person_ids", filters.employeeIds);
  return params;
}

function mapPassage(value) {
  return {
    id: value.session_id, code: value.qr_payload || "", nonce: value.nonce || "",
    direction: directionToUi(value.direction), expiresAt: value.expires_at,
    status: ({ pending: "waiting", signed: "confirmed", decided: "used" })[value.status] || "waiting",
    personName: value.person?.fio || null, decision: value.decision || null, reason: value.reason || null,
  };
}

export const backendApi = {
  groups: () => request("/groups"),
  createGroup: (name, parentId) => request("/groups", { method: "POST", body: { name, parent_id: parentId ? Number(parentId) : null } }),
  employees: async () => (await request("/persons")).map(mapPerson),
  async saveEmployee(id, form) {
    const body = new FormData();
    body.set("fio", String(form.get("name") || "").trim());
    body.set("position", String(form.get("role") || "").trim());
    const groupId = form.get("groupId");
    if (groupId) body.set("group_id", String(groupId));
    const photo = form.get("referencePhoto");
    if (photo instanceof Blob && photo.size) body.set("photo", photo, photo.name || "reference.jpg");
    const person = await request(id ? `/persons/${encodeURIComponent(id)}` : "/persons", { method: id ? "PATCH" : "POST", body });
    return { employee: mapPerson(person) };
  },
  enrollmentCode: async id => {
    const value = await request(`/persons/${encodeURIComponent(id)}/enroll-code`, { method: "POST" });
    return { code: value.code, enrollUrl: value.enroll_url, expiresAt: value.expires_at };
  },
  revokeDevice: () => { throw new ApiError("В текущем API нет маршрута отзыва устройства. Обратитесь к администратору бэкенда."); },
  deactivate: async id => {
    const body = new FormData(); body.set("status", "blocked");
    return mapPerson(await request(`/persons/${encodeURIComponent(id)}`, { method: "PATCH", body }));
  },
  attempts: async () => (await request("/events?limit=200")).map(mapEvent),
  createChallenge: async direction => mapPassage(await request("/passages", { method: "POST", body: { direction: directionToBackend(direction) } })),
  challenge: async id => mapPassage(await request(`/passages/${encodeURIComponent(id)}`)),
  async decidePass(sessionId, direction, frames) {
    if (!Array.isArray(frames) || frames.length !== 3 || frames.some(frame => !(frame instanceof Blob) || !frame.size)) {
      throw new ApiError("Для проверки лица нужны три кадра с камеры.");
    }
    const body = new FormData();
    frames.forEach((frame, index) => body.append("frames", frame, `frame-${index + 1}.jpg`));
    const value = await request(`/passages/${encodeURIComponent(sessionId)}/face`, { method: "POST", body });
    return { employeeName: value.person?.fio || "", direction, outcome: value.decision === "allow" ? "allowed" : "denied",
      reason: value.reason || (value.decision === "allow" ? "Решение получено от сервера" : "Сервер отказал в проходе"),
      reasonCode: value.reason_code || null, shiftId: value.shift?.id ?? null };
  },
  async me() {
    const device = await loadDevice();
    return { employee: device?.deviceId ? { id: String(device.person?.id || ""), name: device.person?.fio || "Рабочий",
      enrolled: true, status: null, shiftId: null, verificationUnknown: true } : null };
  },
  async enroll(code) {
    let device = await loadDevice();
    if (device?.deviceId) throw new ApiError("Ключ уже сохранён на телефоне. Для замены нужен отзыв на сервере.");
    if (!device) { device = await createDevice(); await saveDevice(device); }
    const value = await request("/enroll", { method: "POST", body: { code, public_key_jwk: device.publicKey }, auth: "none" });
    await saveDevice({ ...device, deviceId: value.device_id, person: value.person });
    return { employee: { id: String(value.person.id), name: value.person.fio, enrolled: true, status: null } };
  },
  passOptions: raw => {
    const value = parsePassagePayload(raw);
    return { challengeId: value.sessionId, direction: directionToUi(value.direction), nonce: value.nonce, backendDirection: value.direction };
  },
  async confirmPass(challengeId, options) {
    const device = await loadDevice();
    if (!device?.deviceId) throw new ApiError("Сначала привяжите телефон по коду администратора.");
    const signature = await signPassage(device, { sessionId: challengeId, nonce: options.nonce, direction: options.backendDirection });
    const value = await request(`/passages/${encodeURIComponent(challengeId)}/sign`, {
      method: "POST", body: { device_id: device.deviceId, signature }, auth: "none",
    });
    return { status: value.status, employeeName: value.person?.fio || "" };
  },
  async sendLocation(_shiftId, position) {
    const device = await loadDevice();
    if (!device?.deviceId) throw new ApiError("Сначала привяжите телефон по коду администратора.");
    const timestamp = position.capturedAt || new Date().toISOString();
    const payload = JSON.stringify({ ts: timestamp, points: [{ ts: timestamp, lat: position.latitude, lon: position.longitude, accuracy: position.accuracy }] });
    const signature = await signLocations(device, payload);
    const value = await request("/locations", { method: "POST", body: { device_id: device.deviceId, payload, signature }, auth: "none" });
    if (!value.accepted) throw new ApiError("Сервер отклонил координату. Проверьте открытую смену.");
    return value;
  },
  reportPeople: async () => (await request("/site/now")).map(mapSitePerson),
  async dashboard() {
    const [people, events] = await Promise.all([request("/site/now"), request("/events?limit=6")]);
    return { inside: people.map(mapSitePerson), totalEmployees: null, activeShifts: people.length,
      deniedToday: null, recentAttempts: events.map(mapEvent) };
  },
  async analytics(filters) {
    const query = queryFilters(filters);
    const [heatmap, routes] = await Promise.all([
      request(`/analytics/heatmap?${query}`), request(`/analytics/routes?${query}`),
    ]);
    return { metric: "Минуты пребывания в зоне", zones: heatmap.zones.map(zone => ({ id: String(zone.zone_id), name: zone.name,
      count: zone.minutes, peopleCount: zone.people_count, level: zone.level })),
    routes: routes.persons.map(person => ({ employeeId: String(person.person_id), employeeName: person.fio,
      points: person.points.map(point => ({ x: point.x, y: point.y, at: point.ts, zoneId: point.zone_id })) })),
    totalMinutes: heatmap.total_minutes, outsideMinutes: heatmap.outside_minutes };
  },
  shiftReport: async () => ({ available: false, people: [], shifts: [] }),
  personReport: (id, from, to) => request(`/reports/person/${encodeURIComponent(id)}?${new URLSearchParams({ from, to })}`),
  site: async () => {
    const plan = await request("/site/plan");
    return { name: "План объекта", width: plan.width, height: plan.height, zones: plan.zones || [],
      calibration: plan.calibration || [], planImageUrl: /^https?:\/\//.test(plan.image_url || "") ? plan.image_url : null,
      rawImageUrl: plan.image_url || null };
  },
  updatePlan: image => { const body = new FormData(); body.set("image", image, image.name || "plan.png"); return request("/site/plan", { method: "PUT", body }); },
  updateCalibration: points => request("/site/calibration", { method: "PUT", body: { points } }),
  updateZones: zones => request("/zones", { method: "PUT", body: { zones } }),
  updateSite: () => { throw new ApiError("Для нового API настройте план, калибровку и зоны отдельно."); },
  ask: async question => { const value = await request("/assistant/ask", { method: "POST", body: { question } }); return { status: value.status, answer: value.text, factsUsed: value.facts_used }; },
  summary: async (from, to) => { const value = await request("/assistant/summary", { method: "POST", body: { date_from: from, date_to: to } }); return { status: value.status, answer: value.text, factsUsed: value.facts_used }; },
};

export const api = config.mode === "demo" ? demoApi : backendApi;
configureAuthTransport(request);
