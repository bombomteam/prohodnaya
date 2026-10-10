// Isolated browser-only walkthrough. Never use these outcomes as security evidence.
const KEY = "prohodnaya-demo-v1";
const localDate = (value) => new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Moscow", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(value));
const localHour = (value) => Number(new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Moscow", hour: "2-digit", hourCycle: "h23" }).format(new Date(value)));
const today = () => localDate(Date.now());
const ago = (minutes) => new Date(Date.now() - minutes * 60000).toISOString();
const id = () => Math.random().toString(36).slice(2, 10);
const clone = (value) => structuredClone(value);

function initial() {
  const day = today();
  return {
    employees: [
      { id: "e1", name: "Алексей Морозов", company: "Монолит", role: "Арматурщик", status: "inside", shiftId: "s1", enteredAt: ago(225), enrolled: true },
      { id: "e2", name: "Ирина Павлова", company: "Монолит", role: "Инженер", status: "inside", shiftId: "s2", enteredAt: ago(98), enrolled: true },
      { id: "e3", name: "Тимур Соколов", company: "Стройсервис", role: "Электрик", status: "outside", enrolled: true },
      { id: "e4", name: "Мария Белова", company: "Стройсервис", role: "Мастер", status: "outside", enrolled: false },
    ],
    attempts: [
      { id: "a1", at: ago(98), employeeId: "e2", employeeName: "Ирина Павлова", direction: "entry", outcome: "allowed", reason: "Личность подтверждена" },
      { id: "a2", at: ago(155), employeeId: "e3", employeeName: "Тимур Соколов", direction: "entry", outcome: "denied", reason: "Подтверждение с телефона не получено вовремя" },
      { id: "a3", at: ago(225), employeeId: "e1", employeeName: "Алексей Морозов", direction: "entry", outcome: "allowed", reason: "Личность подтверждена" },
    ],
    shifts: [
      { id: "s1", employeeId: "e1", startedAt: ago(225), endedAt: null },
      { id: "s2", employeeId: "e2", startedAt: ago(98), endedAt: null },
    ],
    points: [
      { employeeId: "e1", shiftId: "s1", at: ago(210), x: 17, y: 78 },
      { employeeId: "e1", shiftId: "s1", at: ago(180), x: 35, y: 52 },
      { employeeId: "e1", shiftId: "s1", at: ago(120), x: 50, y: 47 },
      { employeeId: "e1", shiftId: "s1", at: ago(55), x: 65, y: 50 },
      { employeeId: "e2", shiftId: "s2", at: ago(88), x: 15, y: 75 },
      { employeeId: "e2", shiftId: "s2", at: ago(45), x: 18, y: 47 },
      { employeeId: "e2", shiftId: "s2", at: ago(12), x: 43, y: 18 },
    ],
    challenges: [],
    site: { name: "Строительная площадка № 1", latMin: 55.7500, latMax: 55.7520, lonMin: 37.6150, lonMax: 37.6190,
      zones: ["Проходная", "Бытовки", "Место для курения", "Северный сектор", "Корпус", "Бетономешалка", "Котлован", "Южный сектор", "Резерв"] },
    day,
  };
}

function read() {
  try {
    const value = JSON.parse(localStorage.getItem(KEY));
    if (value?.employees && value?.site) return value;
  } catch { /* reset a broken demo fixture */ }
  const value = initial();
  localStorage.setItem(KEY, JSON.stringify(value));
  return value;
}
function write(value) { localStorage.setItem(KEY, JSON.stringify(value)); return clone(value); }
function fail(message) { throw new Error(message); }
function person(state, employeeId) { return state.employees.find(x => x.id === employeeId); }

export const demoApi = {
  async employees() { return clone(read().employees); },
  async saveEmployee(employeeId, form) {
    const state = read();
    const name = String(form.get("name") || "").trim();
    if (!name) fail("Введите ФИО сотрудника.");
    const file = form.get("referencePhoto");
    if (!employeeId && !(file instanceof File && file.size)) fail("Добавьте эталонное фото.");
    let employee = person(state, employeeId);
    if (employeeId && !employee) fail("Сотрудник не найден.");
    if (!employee) {
      employee = { id: id(), status: "outside", enrolled: false };
      state.employees.push(employee);
    }
    Object.assign(employee, { name, company: String(form.get("company") || ""), role: String(form.get("role") || "") });
    write(state);
    return { employee: clone(employee), photoCheck: { status: "demo", message: "Демо: сотрудник добавлен. Фото не проверено и не сохранено; для этого нужен сервер." } };
  },
  async attempts() { return clone(read().attempts); },
  async createChallenge(direction) {
    const state = read();
    const challenge = { id: id(), code: String(Math.floor(100000 + Math.random() * 900000)), direction,
      expiresAt: new Date(Date.now() + 2 * 60000).toISOString(), status: "waiting" };
    state.challenges.push(challenge); write(state); return clone(challenge);
  },
  async challenge(challengeId) {
    const challenge = read().challenges.find(x => x.id === challengeId);
    if (!challenge) fail("Запрос прохода не найден.");
    if (new Date(challenge.expiresAt) < new Date() && challenge.status === "waiting") return { ...challenge, status: "expired" };
    return clone(challenge);
  },
  async decidePass(challengeId, direction, image) {
    const state = read();
    const challenge = state.challenges.find(x => x.id === challengeId);
    if (!challenge) fail("Запрос прохода не найден.");
    if (challenge.status !== "confirmed" || !challenge.employeeId) fail("Сначала нужно подтверждение с телефона рабочего.");
    if (new Date(challenge.expiresAt) < new Date()) fail("Время подтверждения истекло. Создайте новый запрос.");
    if (challenge.direction !== direction) fail("Операция изменилась. Начните заново.");
    if (!(image instanceof Blob) || !image.size) fail("Снимок с камеры не получен.");
    const employee = person(state, challenge.employeeId);
    const allowed = direction === "entry" ? employee.status === "outside" : employee.status === "inside";
    const reason = !allowed ? (direction === "entry" ? "Смена уже открыта" : "Открытой смены нет") : "Демо: проход разрешён по сценарию. Сравнение лиц выполняется только сервером.";
    const attempt = { id: id(), at: new Date().toISOString(), employeeId: employee.id, employeeName: employee.name, direction, outcome: allowed ? "allowed" : "denied", reason };
    state.attempts.unshift(attempt); challenge.status = "used";
    if (allowed && direction === "entry") {
      const shiftId = id();
      state.shifts.push({ id: shiftId, employeeId: employee.id, startedAt: attempt.at, endedAt: null });
      Object.assign(employee, { status: "inside", shiftId, enteredAt: attempt.at });
    } else if (allowed) {
      const shift = state.shifts.find(x => x.id === employee.shiftId);
      if (shift) shift.endedAt = attempt.at;
      Object.assign(employee, { status: "outside", shiftId: null, enteredAt: null });
    }
    write(state);
    return { ...attempt, employee: clone(employee) };
  },
  async me() { return { employee: null, demoEmployees: clone(read().employees) }; },
  async enrollmentOptions() { return { demo: true }; },
  async enroll(employeeId) {
    const state = read(); const employee = person(state, employeeId);
    if (!employee) fail("Сотрудник не найден.");
    employee.enrolled = true; write(state); return { employee: clone(employee) };
  },
  async passOptions(code) {
    const challenge = read().challenges.find(x => x.code === code && x.status === "waiting");
    if (!challenge || new Date(challenge.expiresAt) < new Date()) fail("Код не найден или время ожидания истекло.");
    return { challengeId: challenge.id, direction: challenge.direction, demo: true };
  },
  async confirmPass(challengeId, assertion) {
    const state = read(); const challenge = state.challenges.find(x => x.id === challengeId);
    if (!challenge || challenge.status !== "waiting") fail("Запрос прохода недоступен.");
    const employee = person(state, assertion.demoEmployeeId);
    if (!employee?.enrolled) fail("Телефон сотрудника ещё не привязан.");
    challenge.employeeId = employee.id; challenge.status = "confirmed";
    write(state); return { status: "confirmed", employeeName: employee.name };
  },
  async sendLocation(shiftId, position) {
    const state = read(); const shift = state.shifts.find(x => x.id === shiftId && !x.endedAt);
    if (!shift) fail("Смена закрыта. Координаты больше не принимаются.");
    const { latMin, latMax, lonMin, lonMax } = state.site;
    const x = (position.longitude - lonMin) / (lonMax - lonMin) * 100;
    const y = (latMax - position.latitude) / (latMax - latMin) * 100;
    state.points.push({ employeeId: shift.employeeId, shiftId, at: position.capturedAt, x, y });
    write(state); return { accepted: true };
  },
  async dashboard() {
    const state = read(); const inside = state.employees.filter(x => x.status === "inside");
    return { inside: clone(inside), totalEmployees: state.employees.length, activeShifts: inside.length,
      deniedToday: state.attempts.filter(x => x.outcome === "denied" && localDate(x.at) === today()).length,
      recentAttempts: clone(state.attempts.slice(0, 6)) };
  },
  async analytics(filters) {
    const state = read();
    const selected = filters.employeeIds ? filters.employeeIds.split(",") : [];
    const points = state.points.filter(x => {
      const date = localDate(x.at); const hour = localHour(x.at);
      return date >= filters.from && date <= filters.to && hour >= Number(filters.hourFrom) && hour <= Number(filters.hourTo)
        && (!selected.length || selected.includes(x.employeeId));
    });
    const zones = state.site.zones.map((name, index) => ({ id: `z${index}`, name, count: points.filter(x => x.x >= 0 && x.x <= 100 && x.y >= 0 && x.y <= 100 && Math.min(2, Math.floor(x.x / (100 / 3))) + Math.min(2, Math.floor(x.y / (100 / 3))) * 3 === index).length }));
    const routes = [...new Set(points.map(x => x.employeeId))].map(employeeId => ({ employeeId, employeeName: person(state, employeeId)?.name || "Сотрудник", points: points.filter(x => x.employeeId === employeeId).sort((a, b) => a.at.localeCompare(b.at)) }));
    return { metric: "Число полученных точек геолокации в зоне", zones, routes, totalPoints: points.length };
  },
  async shiftReport(filters) {
    const state = read();
    const selected = filters.employeeIds ? filters.employeeIds.split(",") : [];
    const shifts = state.shifts.filter(x => {
      const date = localDate(x.startedAt);
      return date >= filters.from && date <= filters.to && (!selected.length || selected.includes(x.employeeId));
    }).map(x => ({ ...x, employeeName: person(state, x.employeeId)?.name || "Сотрудник",
      workedMinutes: Math.max(0, Math.floor((new Date(x.endedAt || Date.now()) - new Date(x.startedAt)) / 60000)) }));
    const people = [...new Set(shifts.map(x => x.employeeId))].map(employeeId => ({ employeeId,
      employeeName: person(state, employeeId)?.name || "Сотрудник", shiftCount: shifts.filter(x => x.employeeId === employeeId).length,
      workedMinutes: shifts.filter(x => x.employeeId === employeeId).reduce((sum, x) => sum + x.workedMinutes, 0) }));
    return { shifts, people };
  },
  async site() { return clone(read().site); },
  async updateSite(site) {
    const numbers = [site.latMin, site.latMax, site.lonMin, site.lonMax].map(Number);
    if (numbers.some(x => !Number.isFinite(x)) || numbers[0] >= numbers[1] || numbers[2] >= numbers[3]) fail("Проверьте границы площадки: минимум должен быть меньше максимума.");
    const state = read(); state.site = { ...state.site, ...site, latMin: numbers[0], latMax: numbers[1], lonMin: numbers[2], lonMax: numbers[3] };
    write(state); return clone(state.site);
  },
  async ask() { return { status: "unavailable", answer: "В деморежиме ответы помощника отключены. Для ответа на реальных данных нужен сервер аналитики и модель." }; },
  async summary() { return { status: "unavailable", answer: "Сводка появится после подключения сервера аналитики и модели. Деморежим не придумывает показатели." }; },
};
