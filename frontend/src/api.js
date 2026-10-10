import { config } from "../config.js";
import { accessToken, configureAuthTransport, session } from "./auth.js";
import { loadDevice, saveDevice, createDevice, signPassCode, signLocations } from "./device.js";
import { demoApi } from "./demo-api.js";
import { caseZoneById } from "./zones.js";

export class ApiError extends Error {
  constructor(message, status, code) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

// Бэкенд отдаёт адреса файлов относительно корня сервера (/api/files/plan/plan.png): достраиваем их от адреса API.
// Принимаются только http(s)-адреса; всё остальное (javascript:, data:) отбрасывается.
export function resolveApiUrl(value, base = config.apiBaseUrl, origin = globalThis.location?.href) {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const resolved = new URL(value.trim(), new URL(base, origin));
    return resolved.protocol === "http:" || resolved.protocol === "https:" ? resolved.href : null;
  } catch { return null; }
}

// Начало текущих суток в часовом поясе объекта, в виде ISO-времени с зоной (бэкенд принимает только aware-даты).
export function startOfDayIso(now = new Date(), timeZone = config.timeZone || "Europe/Moscow") {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }).formatToParts(now).map(part => [part.type, part.value]));
  const wallClockAsUtc = Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day), Number(parts.hour), Number(parts.minute), Number(parts.second));
  const offsetMs = wallClockAsUtc - Math.floor(now.getTime() / 1000) * 1000;
  return new Date(Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day)) - offsetMs).toISOString();
}

export async function request(path, { method = "GET", body, signal, auth = "operator", headers = {}, timeoutMs = config.requestTimeoutMs || 15000 } = {}) {
  if (auth === "operator") {
    const token = await accessToken();
    if (token) headers = { ...headers, Authorization: `Bearer ${token}` };
  }
  const timeout = AbortSignal.timeout(timeoutMs);
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
    const message = typeof data?.message === "string" && data.message.trim() ? data.message :
      typeof data?.detail === "string" && data.detail.trim() ? data.detail :
      response.status === 401 ? "Сессия истекла, войдите заново" : response.status === 403 ? "Недостаточно прав" :
      "Не удалось выполнить действие. Повторите попытку.";
    if (response.status === 401 && auth === "operator" && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("operator-session-expired", { detail: { message } }));
    }
    throw new ApiError(message, response.status, data?.code);
  }
  return data;
}

const directionToBackend = direction => direction === "entry" ? "in" : "out";
const directionToUi = direction => direction === "in" ? "entry" : "exit";

export function reasonText(value, fallback = "Сервер не сообщил причину") {
  const text = value?.reason?.text || value?.message || value?.reason;
  return typeof text === "string" && text.trim() ? text : fallback;
}

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
    outcome: event.result === "allow" ? "allowed" : "denied", reason: reasonText(event, event.result === "allow" ? "Разрешено сервером" : "Отказ сервера"),
    reasonCode: event.reason?.code || event.reason_code || null,
  };
}

// Each accepted route point is a recorded sighting, not proof of continuous presence.
export function appearancesFromRoutes(routes, zones) {
  return routes.flatMap(route => route.points.map(point => ({
    employeeId: String(route.employeeId), employeeName: route.employeeName,
    at: point.at, zoneName: caseZoneById(point.zoneId, zones) || "Вне трёх заданных зон",
  }))).filter(entry => entry.at && !Number.isNaN(Date.parse(entry.at)))
    .sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
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

// Сессия прохода: pending → ждём код, signed → код принят (можно снимать лицо), decided → решение принято.
function mapPassage(value) {
  return {
    id: value.session_id, code: "", direction: directionToUi(value.direction), expiresAt: value.expires_at,
    status: ({ pending: "waiting", signed: "confirmed", decided: "used" })[value.status] || "waiting",
    personName: value.person?.fio || null, decision: value.decision || null, reason: reasonText(value, ""),
    reasonCode: value.reason_code || null,
  };
}

const assistantAnswer = value => ({ status: value.status, answer: value.text, factsUsed: value.facts_used, provider: value.provider, model: value.model });
const assistantInfo = value => ({ switchable: true, provider: value.provider, model: value.model, configured: Boolean(value.configured), available: Boolean(value.available) });

export const backendApi = {
  groups: () => request("/groups"),
  createGroup: (name, parentId) => request("/groups", { method: "POST", body: { name, parent_id: parentId ? Number(parentId) : null } }),
  employees: async () => (await request("/persons")).map(mapPerson),
  async saveEmployee(id, form) {
    const body = new FormData();
    body.set("fio", String(form.get("name") || "").trim());
    body.set("position", String(form.get("role") || "").trim());
    const groupId = form.get("groupId");
    if (groupId || id) body.set("group_id", String(groupId || ""));
    const photo = form.get("referencePhoto");
    if (photo instanceof Blob && photo.size) body.set("photo", photo, photo.name || "reference.jpg");
    const person = await request(id ? `/persons/${encodeURIComponent(id)}` : "/persons", { method: id ? "PATCH" : "POST", body,
      timeoutMs: config.faceRequestTimeoutMs || 35000 });
    return { employee: mapPerson(person) };
  },
  enrollmentCode: async id => {
    const value = await request(`/persons/${encodeURIComponent(id)}/enroll-code`, { method: "POST" });
    return { code: value.code, enrollUrl: value.enroll_url, expiresAt: value.expires_at };
  },
  revokeDevice: () => { throw new ApiError("Отвязать телефон из этого окна пока нельзя. Обратитесь к администратору системы."); },
  deactivate: async id => {
    const body = new FormData(); body.set("status", "blocked");
    return mapPerson(await request(`/persons/${encodeURIComponent(id)}`, { method: "PATCH", body }));
  },
  attempts: async () => (await request("/events?limit=200")).map(mapEvent),
  // Контролёр: поиск человека по ФИО (GET /passages/people), сессия на человека, ввод 6-значного кода рабочего.
  gatePeople: async query => (await request(`/passages/people?${new URLSearchParams({ ...(query ? { q: query } : {}), limit: "50" })}`)).map(mapPerson),
  createChallenge: async (direction, personId) => mapPassage(await request("/passages", { method: "POST",
    body: { person_id: Number(personId), direction: directionToBackend(direction) } })),
  challenge: async id => mapPassage(await request(`/passages/${encodeURIComponent(id)}`)),
  async verifyCode(sessionId, code) {
    const value = await request(`/passages/${encodeURIComponent(sessionId)}/code`, { method: "POST", body: { code } });
    // Неверный код: HTTP 200, сессия остаётся pending, причина в reason (decision не выставлен).
    if (value.status === "pending" && value.reason_code) throw new ApiError(reasonText(value, "Код не совпадает"), 200, value.reason_code);
    return mapPassage(value);
  },
  async decidePass(sessionId, direction, frames) {
    if (!Array.isArray(frames) || frames.length < 1 || frames.length > 10 || frames.some(frame => !(frame instanceof Blob) || !frame.size)) {
      throw new ApiError("Для проверки лица отправьте от 1 до 10 непустых кадров с камеры.");
    }
    const body = new FormData();
    frames.forEach((frame, index) => body.append("frames", frame, `frame-${index + 1}.jpg`));
    const value = await request(`/passages/${encodeURIComponent(sessionId)}/face`, { method: "POST", body, auth: "operator",
      timeoutMs: config.faceRequestTimeoutMs || 35000 });
    return { employeeName: value.person?.fio || "", direction, outcome: value.decision === "allow" ? "allowed" : "denied",
      reason: reasonText(value, value.decision === "allow" ? "Решение получено от сервера" : "Сервер отказал в проходе"),
      reasonCode: value.reason?.code || value.reason_code || null, shiftId: value.shift?.id ?? null };
  },
  async me() {
    const device = await loadDevice();
    return { employee: device?.deviceId ? { id: String(device.person?.id || ""), name: device.person?.fio || "Рабочий",
      enrolled: true, status: null, shiftId: null, verificationUnknown: true } : null };
  },
  async enroll(code) {
    let device = await loadDevice();
    if (device?.deviceId) throw new ApiError("Этот телефон уже привязан. Чтобы привязать заново, администратор должен сначала отвязать его.");
    if (!device) { device = await createDevice(); await saveDevice(device); }
    const value = await request("/enroll", { method: "POST", body: { code, public_key_jwk: device.publicKey }, auth: "none" });
    await saveDevice({ ...device, deviceId: value.device_id, person: value.person });
    return { employee: { id: String(value.person.id), name: value.person.fio, enrolled: true, status: null } };
  },
  // Телефон: подписанный ключом устройства запрос кода ждущего прохода. null — контролёр ещё не создал проход.
  async workerPassCode() {
    const device = await loadDevice();
    if (!device?.deviceId) throw new ApiError("Сначала привяжите телефон по коду администратора.");
    const ts = new Date().toISOString();
    const signature = await signPassCode(device, ts);
    const value = await request("/device/pass-code", { method: "POST", body: { device_id: device.deviceId, ts, signature }, auth: "none" });
    if (!value.pending) return null;
    return { id: value.session_id, direction: directionToUi(value.direction), code: value.code, expiresAt: value.expires_at,
      personName: device.person?.fio || "" };
  },
  async sendLocation(_shiftId, position) {
    const device = await loadDevice();
    if (!device?.deviceId) throw new ApiError("Сначала привяжите телефон по коду администратора.");
    const timestamp = position.capturedAt || new Date().toISOString();
    const payload = JSON.stringify({ ts: timestamp, points: [{ ts: timestamp, lat: position.latitude, lon: position.longitude, accuracy: position.accuracy }] });
    const signature = await signLocations(device, payload);
    const value = await request("/locations", { method: "POST", body: { device_id: device.deviceId, payload, signature }, auth: "none" });
    if (!value.accepted) throw new ApiError("Сервер не принял координаты. Проверьте, что смена открыта.");
    return value;
  },
  reportPeople: async () => (await request("/site/now")).map(mapSitePerson),
  // Сводка обстановки. Списки и счётчики берём из доступных роли маршрутов; чего роль получить не может, остаётся null
  // и показывается в интерфейсе как «нет данных», а не как ноль.
  async dashboard() {
    const optional = promise => promise.catch(() => null);
    const [people, events, today, persons] = await Promise.all([
      request("/site/now"), request("/events?limit=6"),
      // Все попытки за сегодня одним запросом: из них считаем и разрешённые, и отказы.
      optional(request(`/events?${new URLSearchParams({ from: startOfDayIso(), limit: "1000" })}`)),
      session.roles.includes("admin") ? optional(request("/persons")) : null,
    ]);
    const todayEvents = Array.isArray(today) ? today : null;
    return { inside: people.map(mapSitePerson), activeShifts: people.length,
      totalEmployees: Array.isArray(persons) ? persons.length : null,
      deniedToday: todayEvents ? todayEvents.filter(event => event.result === "deny").length : null,
      allowedToday: todayEvents ? todayEvents.filter(event => event.result === "allow").length : null,
      todayCapped: Boolean(todayEvents && todayEvents.length >= 1000), recentAttempts: events.map(mapEvent) };
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
      calibration: plan.calibration || [], planImageUrl: resolveApiUrl(plan.image_url),
      rawImageUrl: plan.image_url || null };
  },
  updatePlan: image => { const body = new FormData(); body.set("image", image, image.name || "plan.png"); return request("/site/plan", { method: "PUT", body }); },
  updateCalibration: points => request("/site/calibration", { method: "PUT", body: { points } }),
  updateZones: zones => request("/zones", { method: "PUT", body: { zones } }),
  updateSite: () => { throw new ApiError("План, калибровка и зоны настраиваются отдельно."); },
  ask: async question => assistantAnswer(await request("/assistant/ask", { method: "POST", body: { question } })),
  summary: async (from, to) => assistantAnswer(await request("/assistant/summary", { method: "POST", body: { date_from: from, date_to: to } })),
  assistantStatus: async () => {
    try { return assistantInfo(await request("/assistant/status")); } catch (error) { if (error.status === 404) return { switchable: false, unsupported: true }; throw error; }
  },
  setAssistantProvider: async provider => {
    try { return assistantInfo(await request("/assistant/provider", { method: "PUT", body: { provider } })); } catch (error) { if (error.status === 404) return { switchable: false, unsupported: true }; throw error; }
  },
};

export const api = config.mode === "demo" ? demoApi : backendApi;
configureAuthTransport(request);
