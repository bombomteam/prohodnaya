// «Камера на входе»: живое видео камеры gate-1 с рамками модуля распознавания.
// Кадры берутся по одному (GET /api/camera/frame.jpg через src/api.js, с токеном оператора) примерно 8 раз в секунду,
// только пока блок на экране и вкладка активна; следующий запрос уходит после завершения предыдущего.
// Состояние камеры — GET /api/camera/status раз в 3 секунды. Камера офлайн (503) — плашка и повтор раз в 3 секунды.
// Модуль сам меняет картинку и статус в DOM, не вызывая общий render(); html() всегда строит ту же разметку,
// что уже стоит на странице, поэтому сверка DOM в view.js ничего не сбрасывает.

const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

export const CAMERA_TEXT = {
  title: "Камера на входе",
  hint: "Видео с iPhone на проходной, рамки рисует модуль распознавания",
  connecting: "Подключаемся к камере…",
  offline: "Камера офлайн — включите трансляцию на iPhone (Larix)",
  error: "Нет связи с камерой. Пробуем подключиться снова…",
  demo: "Камера доступна только на стенде",
};

export function createCameraLive({
  enabled = true, fetchFrame, fetchStatus,
  frameIntervalMs = 125, statusIntervalMs = 3000, offlineRetryMs = 3000,
  doc = globalThis.document, urls = globalThis.URL, Observer = globalThis.IntersectionObserver,
  timers = globalThis, now = () => Date.now(),
} = {}) {
  // mode: connecting — ждём первый кадр; live — кадр показан; offline — сервер ответил 503; error — другая ошибка.
  const state = { mode: "connecting", url: null, status: null };
  let element = null, observer = null, listening = false, inView = true, generation = 0;
  let frameTimer = null, statusTimer = null, frameBusy = false, statusBusy = false, frameAbort = null, statusAbort = null;

  const pageVisible = () => !doc || doc.visibilityState !== "hidden";
  const active = () => Boolean(element) && inView && pageVisible();

  function view() {
    if (state.mode === "live") {
      const fps = Number(state.status?.fps);
      return { tone: "online", label: state.status?.fps != null && Number.isFinite(fps) && fps > 0 ? `В эфире · ${Math.round(fps)} к/с` : "В эфире", note: "", src: state.url };
    }
    if (state.mode === "offline") return { tone: "offline", label: "Офлайн", note: CAMERA_TEXT.offline, src: null };
    if (state.mode === "error") return { tone: "offline", label: "Нет связи", note: CAMERA_TEXT.error, src: null };
    return { tone: "wait", label: "Подключение…", note: CAMERA_TEXT.connecting, src: null };
  }

  function html() {
    const head = (tone, label, ids) => `<div class="camera-live-head"><h2>${CAMERA_TEXT.title}</h2><span class="camera-live-status is-${tone}"${ids ? ' id="camera-live-status"' : ""}><i class="camera-live-dot" aria-hidden="true"></i><span${ids ? ' id="camera-live-status-text"' : ""}>${esc(label)}</span></span></div>`;
    const hint = `<p class="camera-live-hint">${CAMERA_TEXT.hint}</p>`;
    if (!enabled) {
      return `<section class="panel camera-live camera-live-demo" id="camera-live" aria-label="${CAMERA_TEXT.title}">${head("wait", "Демо", false)}<div class="camera-live-frame"><div class="camera-live-note">${CAMERA_TEXT.demo}</div></div>${hint}</section>`;
    }
    const v = view();
    return `<section class="panel camera-live" id="camera-live" aria-label="${CAMERA_TEXT.title}">${head(v.tone, v.label, true)}<div class="camera-live-frame"><img class="camera-live-img" id="camera-live-img" alt="Камера на входе: последний кадр с рамками распознавания" ${v.src ? `src="${esc(v.src)}"` : "hidden"}><div class="camera-live-note" id="camera-live-note"${v.note ? "" : " hidden"}>${esc(v.note)}</div></div>${hint}</section>`;
  }

  // Точечное обновление уже нарисованного блока: те же атрибуты и текст, что дал бы html().
  function apply() {
    if (!element?.querySelector) return;
    const v = view();
    const img = element.querySelector("#camera-live-img"), status = element.querySelector("#camera-live-status");
    const label = element.querySelector("#camera-live-status-text"), note = element.querySelector("#camera-live-note");
    if (img) {
      if (v.src) { if (img.getAttribute("src") !== v.src) img.setAttribute("src", v.src); img.hidden = false; }
      else { img.removeAttribute("src"); img.hidden = true; }
    }
    if (status) status.className = `camera-live-status is-${v.tone}`;
    if (label && label.textContent !== v.label) label.textContent = v.label;
    if (note) { note.hidden = !v.note; if (note.textContent !== v.note) note.textContent = v.note; }
  }

  function showFrame(blob) {
    const previous = state.url;
    state.url = urls.createObjectURL(blob);
    state.mode = "live";
    apply();
    if (previous) urls.revokeObjectURL(previous);
  }

  function showFailure(error) {
    const previous = state.url;
    state.url = null;
    state.mode = error?.status === 503 || error?.code === "CAMERA_OFFLINE" ? "offline" : "error";
    apply();
    if (previous) urls.revokeObjectURL(previous);
  }

  function scheduleFrame(delay) {
    timers.clearTimeout(frameTimer); frameTimer = null;
    if (active()) frameTimer = timers.setTimeout(pollFrame, delay);
  }

  async function pollFrame() {
    frameTimer = null;
    if (!active() || frameBusy) return;
    frameBusy = true;
    const run = generation, started = now();
    const abort = frameAbort = new AbortController();
    let delay = offlineRetryMs;
    try {
      const blob = await fetchFrame({ signal: abort.signal });
      if (run !== generation) return;
      if (!blob || !blob.size || (blob.type && !/^image\//.test(blob.type))) throw new Error("Сервер не прислал кадр камеры.");
      showFrame(blob);
      delay = Math.max(0, frameIntervalMs - (now() - started));
    } catch (error) {
      if (run !== generation) return;
      showFailure(error);
    } finally {
      if (run === generation) { frameBusy = false; frameAbort = null; }
    }
    scheduleFrame(delay);
  }

  async function pollStatus() {
    statusTimer = null;
    if (!active() || statusBusy) return;
    statusBusy = true;
    const run = generation;
    const abort = statusAbort = new AbortController();
    try {
      const value = await fetchStatus({ signal: abort.signal });
      if (run !== generation) return;
      state.status = value && typeof value === "object" ? value : null;
    } catch {
      if (run !== generation) return;
      state.status = null;
    } finally {
      if (run === generation) { statusBusy = false; statusAbort = null; }
    }
    apply();
    // Камера снова в эфире, а кадры ждут редкого повтора: берём кадр сразу.
    if (state.status?.online && state.mode !== "live" && !frameBusy && active()) { timers.clearTimeout(frameTimer); frameTimer = null; pollFrame(); }
    if (active()) statusTimer = timers.setTimeout(pollStatus, statusIntervalMs);
  }

  // Пауза: таймеры снимаются, начатый запрос завершится сам и новый не запланирует.
  function pause() {
    timers.clearTimeout(frameTimer); timers.clearTimeout(statusTimer);
    frameTimer = null; statusTimer = null;
  }
  function resume() {
    if (!active()) return;
    if (!frameBusy && frameTimer === null) pollFrame();
    if (!statusBusy && statusTimer === null) pollStatus();
  }
  const onVisibility = () => { if (active()) resume(); else pause(); };
  function onIntersect(entries) {
    for (const entry of entries) if (entry.target === element) inView = Boolean(entry.isIntersecting);
    if (active()) resume(); else pause();
  }

  // Полная остановка: таймеры, запросы, наблюдатели и blob URL освобождаются, состояние сбрасывается.
  function stop() {
    generation += 1;
    pause();
    frameAbort?.abort(); statusAbort?.abort();
    frameAbort = null; statusAbort = null; frameBusy = false; statusBusy = false;
    observer?.disconnect(); observer = null;
    if (listening) { doc?.removeEventListener?.("visibilitychange", onVisibility); listening = false; }
    element = null; inView = true;
    if (state.url) urls.revokeObjectURL(state.url);
    state.mode = "connecting"; state.url = null; state.status = null;
  }

  // Вызывается после каждой перерисовки: находит блок на странице, запускает или останавливает опрос.
  function sync(root) {
    if (!enabled) return;
    const next = root?.querySelector?.("#camera-live") || null;
    if (!next) { if (element) stop(); return; }
    if (next !== element) {
      element = next; inView = true;
      if (!listening && doc?.addEventListener) { doc.addEventListener("visibilitychange", onVisibility); listening = true; }
      observer?.disconnect(); observer = null;
      if (typeof Observer === "function") { observer = new Observer(onIntersect); observer.observe(element); }
      apply();
    }
    resume();
  }

  return { html, sync, stop, snapshot: () => ({ mode: state.mode, url: state.url, status: state.status, active: active() }) };
}
