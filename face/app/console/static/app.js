// Консоль модуля распознавания лиц. Без сборщика и внешних библиотек.
// Все адреса относительные (страница открыта на /console/), чтобы консоль работала и за прокси бэкенда.
"use strict";

const $ = (id) => document.getElementById(id);
const api = (path) => new URL("../" + path, location.href).toString();

const QUALITY_SHORT = {
  ok: "пригоден", no_face: "нет лица", multiple_faces: "несколько лиц", face_covered: "лицо закрыто",
  too_small: "далеко", turn_to_camera: "повернитесь", low_quality: "нечётко/темно",
};
const WARN_CODES = new Set(["no_face", "turn_to_camera", "too_small", "low_quality", "no_frames", "face_covered"]);

const state = {
  cameras: [],
  camera: null,
  tab: "people",
  es: null,
  liveThr: { match: null, passive: 0.5 },
  file: null, // ответ analyze
  sel: 0,
};

function fmt(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  if (typeof v === "number") return v.toFixed(digits);
  return String(v);
}
function yesNo(v) { return v === true ? "да" : v === false ? "нет" : "—"; }
function setStatus(el, cls, text, small) {
  el.className = "status-line" + (cls ? " " + cls : "");
  el.textContent = text;
  if (small) {
    const s = document.createElement("small");
    s.textContent = small;
    el.appendChild(s);
  }
}
function fillFacts(dl, rows) {
  dl.replaceChildren();
  for (const [k, v] of rows) {
    if (v === undefined) continue;
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v === null ? "—" : String(v);
    dl.append(dt, dd);
  }
}
async function readError(resp) {
  try {
    const j = await resp.json();
    if (j && j.reason && j.reason.text) return j.reason.text;
  } catch (_) { /* не JSON */ }
  return "Ошибка сервиса (HTTP " + resp.status + ").";
}

// ------------------------------------------------------------------ камеры

async function loadCameras() {
  let cams = [];
  try {
    const r = await fetch(api("v1/cameras"), { cache: "no-store" });
    if (r.ok) cams = (await r.json()).cameras || [];
  } catch (_) { /* сеть: покажем «нет данных» */ }
  state.cameras = cams;
  const sel = $("camera");
  const ids = cams.map((c) => c.camera_id);
  if (sel.options.length !== ids.length || [...sel.options].some((o, i) => o.value !== ids[i])) {
    sel.replaceChildren(...cams.map((c) => {
      const o = document.createElement("option");
      o.value = c.camera_id; o.textContent = c.camera_id; return o;
    }));
    if (!cams.length) {
      const o = document.createElement("option"); o.textContent = "нет камер"; o.value = ""; sel.append(o);
    }
    const prev = state.camera;
    state.camera = ids.includes(prev) ? prev : (ids[0] || null);
    sel.value = state.camera || "";
    if (prev !== state.camera) restartLive();
  }
  const pill = $("camera-status");
  const cam = cams.find((c) => c.camera_id === state.camera);
  if (!cam) {
    pill.className = "pill pill-gray"; pill.textContent = cams.length ? "нет данных" : "камеры не настроены";
  } else if (cam.online) {
    pill.className = "pill pill-ok";
    const parts = ["в сети"];
    if (cam.fps_in != null) parts.push("вход " + fmt(cam.fps_in, 1) + " к/с");
    if (cam.fps_analyzed != null) parts.push("анализ " + fmt(cam.fps_analyzed, 1) + " к/с");
    if (cam.fps_preview) parts.push("показ " + fmt(cam.fps_preview, 1) + " к/с");
    if (cam.last_frame_age_ms != null) parts.push("задержка " + Math.round(cam.last_frame_age_ms) + " мс");
    pill.textContent = parts.join(" · ");
  } else {
    pill.className = "pill pill-bad"; pill.textContent = "офлайн";
  }
}

// ------------------------------------------------------------------ вкладки и живой поток

function setTab(name) {
  state.tab = name;
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  for (const t of ["people", "faces", "file"]) $("tab-" + t).hidden = t !== name;
  try { localStorage.setItem("face-console-tab", name); } catch (_) { /* приватный режим */ }
  restartLive();
}

function stopLive() {
  if (state.es) { state.es.close(); state.es = null; }
  for (const id of ["people-img", "faces-img"]) {
    const img = $(id);
    img.removeAttribute("src");
    img.hidden = true;
  }
}

function restartLive() {
  stopLive();
  const layer = state.tab === "people" ? "people" : state.tab === "faces" ? "faces" : null;
  if (!layer) return;
  const empty = $(layer + "-empty");
  if (!state.camera) { empty.hidden = false; return; }
  empty.hidden = true;
  const img = $(layer + "-img");
  const cam = encodeURIComponent(state.camera);
  img.src = api(`v1/cameras/${cam}/live.mjpeg?layer=${layer}&_=${Date.now()}`);
  img.hidden = false;
  const es = new EventSource(api(`v1/cameras/${cam}/events`));
  es.addEventListener("frame", (e) => onLiveFrame(JSON.parse(e.data)));
  es.addEventListener("idle", () => onLiveIdle());
  state.es = es;
}

function onLiveIdle() {
  const c = $("people-count");
  c.textContent = "—"; c.className = "big-count none";
  $("people-warn").hidden = true;
  setStatus($("faces-status"), "", "Нет кадров с камеры");
}

function onLiveFrame(f) {
  state.liveThr = { match: f.match_threshold, passive: f.passive_threshold ?? 0.5 };
  // Люди
  const n = f.people_count;
  const c = $("people-count");
  c.textContent = String(n);
  c.className = "big-count" + (n >= 2 ? " many" : n === 0 ? " none" : "");
  $("people-warn").hidden = n < 2;
  $("people-ts").textContent = f.ts ? new Date(f.ts).toLocaleTimeString("ru-RU") : "—";
  const inZone = f.people.filter((p) => p.in_zone).length;
  $("people-boxes").textContent = `${f.people.length} (в зоне ${inZone})`;
  $("people-faces").textContent = String(f.faces.length);
  // Лица
  const best = f.best != null ? f.faces[f.best] : null;
  if (n >= 2) setStatus($("faces-status"), "bad", "В кадре несколько человек", "Проходите по одному.");
  else if (!best) setStatus($("faces-status"), "warn", f.status_text || "Лицо не найдено");
  else if (f.status !== "ok") setStatus($("faces-status"), "warn", f.status_text);
  else setStatus($("faces-status"), "ok", "Лицо пригодно для сравнения");
  $("faces-count").textContent = String(f.faces.length);
  $("faces-quality").textContent = best
    ? `${QUALITY_SHORT[best.quality_status] || best.quality_status}; ${qualityLine(best.quality)}` : "—";
  const pthr = state.liveThr.passive;
  $("faces-live").textContent = f.passive == null ? "—"
    : `${f.passive >= pthr ? "живое" : "подделка?"} ${fmt(f.passive)} (порог ${fmt(pthr)})`;
  $("faces-sim").textContent = f.similarity == null ? (state.refSet ? "нет лица для сравнения" : "эталон не загружен")
    : `${fmt(f.similarity)}` + (f.match_threshold != null
      ? ` — ${f.match ? "совпадает" : "не совпадает"} (порог ${fmt(f.match_threshold)})` : "");
}

function qualityLine(q) {
  if (!q) return "";
  const parts = [];
  if (q.face_px != null) parts.push(`лицо ${Math.round(q.face_px)} px`);
  if (q.yaw != null) parts.push(`поворот ${Math.round(q.yaw)}°`);
  if (q.pitch != null) parts.push(`наклон ${Math.round(q.pitch)}°`);
  if (q.sharpness != null) parts.push(`резкость ${Math.round(q.sharpness)}`);
  if (q.brightness != null) parts.push(`яркость ${Math.round(q.brightness)}`);
  return parts.join(", ");
}

// ------------------------------------------------------------------ демо-эталон

function showRef(info, errorText) {
  const box = $("ref-result");
  box.hidden = false;
  box.replaceChildren();
  const st = document.createElement("div");
  if (errorText) {
    setStatus(st, "bad", errorText);
    state.refSet = false;
  } else if (!info) {
    setStatus(st, "", "Эталон не загружен");
    state.refSet = false;
  } else {
    setStatus(st, info.accepted ? "ok" : "bad", info.accepted ? "Эталон загружен" : "Эталон не принят", info.reason && info.reason.text);
    state.refSet = !!info.accepted;
    const dl = document.createElement("dl"); dl.className = "facts";
    fillFacts(dl, [["Лиц", info.faces_count], ["Людей", info.people_count],
      ["Качество", info.face ? qualityLine(info.face.quality) : null]]);
    box.append(st, dl);
    return;
  }
  box.append(st);
}

async function loadRef() {
  try {
    const r = await fetch(api("v1/console/reference"), { cache: "no-store" });
    if (r.ok) showRef((await r.json()).reference);
  } catch (_) { /* ignore */ }
}

$("ref-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const file = $("ref-file").files[0];
  if (!file) return;
  const fd = new FormData(); fd.append("image", file);
  const btn = e.submitter || e.target.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    const r = await fetch(api("v1/console/reference"), { method: "POST", body: fd });
    if (!r.ok) showRef(null, await readError(r));
    else showRef(await r.json());
  } catch (_) {
    showRef(null, "Сервис недоступен.");
  } finally { btn.disabled = false; }
});
$("ref-clear").addEventListener("click", async () => {
  try { await fetch(api("v1/console/reference"), { method: "DELETE" }); } catch (_) { /* ignore */ }
  $("ref-file").value = "";
  showRef(null);
});

// ------------------------------------------------------------------ проверка файла

$("file-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const media = $("media").files[0];
  if (!media) return;
  const fd = new FormData();
  fd.append("media", media);
  const ref = $("reference").files[0];
  if (ref) fd.append("reference", ref);
  const btn = $("file-submit");
  const prog = $("file-progress");
  btn.disabled = true;
  const t0 = performance.now();
  const timer = setInterval(() => { prog.textContent = `Анализ… ${((performance.now() - t0) / 1000).toFixed(0)} с`; }, 500);
  prog.textContent = "Загрузка…";
  try {
    const r = await fetch(api("v1/console/analyze"), { method: "POST", body: fd });
    if (!r.ok) { prog.textContent = await readError(r); return; }
    const data = await r.json();
    prog.textContent = `Готово: ${data.frames.length} кадр(ов) за ${((performance.now() - t0) / 1000).toFixed(1)} с`;
    renderFile(data);
  } catch (_) {
    prog.textContent = "Сервис недоступен. Попробуйте ещё раз.";
  } finally {
    clearInterval(timer);
    btn.disabled = false;
  }
});

function statusClass(code) {
  if (code === "ok") return "ok";
  return WARN_CODES.has(code) ? "warn" : "bad";
}

function frameClass(f, s) {
  if (f.people_count >= 2) return "bad";
  if (f.passive != null && s.passive_threshold != null && f.passive < s.passive_threshold) return "bad";
  if (f.status !== "ok") return "warn";
  if (f.similarity != null && s.match_threshold != null && f.similarity < s.match_threshold) return "bad";
  return "ok";
}

function renderFile(data) {
  state.file = data;
  $("file-out").hidden = false;
  const s = data.summary || {};
  const res = data.result;
  const rs = $("result-status");
  if (res) {
    setStatus(rs, statusClass(res.status), res.status === "ok" ? "Проверка лица пройдена" : res.reason.text, `код: ${res.status}`);
  } else if (data.reference && !data.reference.accepted) {
    setStatus(rs, "warn", "Эталон не принят — сравнение не выполнялось", data.reference.reason && data.reference.reason.text);
  } else {
    setStatus(rs, "", "Эталон не задан — только факты по кадрам");
  }
  const fx = (res && res.facts) || {};
  const live = fx.liveness || {};
  fillFacts($("result-facts"), [
    ["Тип файла", s.kind === "video" ? "видео" : "фото"],
    ["Кадров", `${s.frames_total} (с лицом ${s.frames_with_face}, пригодных ${s.frames_usable})`],
    ["Людей максимум", s.people_max],
    ["Сходство", res ? fmt(fx.similarity) : fmt(s.similarity_max)],
    ["Порог сходства", fmt(fx.threshold ?? s.match_threshold)],
    ["Совпадение", res ? yesNo(fx.match) : undefined],
    ["«Живость» пассивная", live.passive != null ? `${fmt(live.passive)} (порог ${fmt(live.passive_threshold)})` : undefined],
    ["«Живость» по кадрам", live.sequence != null ? `${fmt(live.sequence)} (порог ${fmt(live.sequence_threshold)})` : undefined],
    ["Живой человек", live.live !== undefined ? yesNo(live.live) : undefined],
    ["Кадр = эталон", res ? yesNo(fx.frame_is_reference) : undefined],
    ["Повтор кадра", res ? yesNo(fx.frame_replayed) : undefined],
    ["Модель", res ? res.model_version : undefined],
    ["Время анализа", `${Math.round(s.analysis_ms || 0)} мс`],
  ]);

  const ref = data.reference;
  if (!ref) {
    setStatus($("ref-check"), "", "Не загружен");
    fillFacts($("ref-facts"), []);
  } else {
    setStatus($("ref-check"), ref.accepted ? "ok" : "bad", ref.accepted ? "Эталон пригоден" : "Эталон не принят", ref.reason && ref.reason.text);
    fillFacts($("ref-facts"), [["Лиц", ref.faces_count], ["Людей", ref.people_count],
      ["Качество", ref.face ? qualityLine(ref.face.quality) : null]]);
  }

  const tl = $("timeline");
  tl.replaceChildren();
  data.frames.forEach((f, i) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "tl";
    const st = document.createElement("div"); st.className = "st " + frameClass(f, s);
    const bar = document.createElement("div"); bar.className = "bar";
    const sim = document.createElement("i"); sim.className = "sim";
    sim.style.height = `${Math.max(0, Math.min(1, f.similarity ?? 0)) * 36}px`;
    const lv = document.createElement("i"); lv.className = "live";
    lv.style.height = `${Math.max(0, Math.min(1, f.passive ?? 0)) * 36}px`;
    bar.append(sim, lv);
    const num = document.createElement("div");
    num.className = "num" + (f.people_count >= 2 ? " many" : "");
    num.textContent = String(f.people_count);
    b.append(st, bar, num);
    b.title = `${(f.ts_ms / 1000).toFixed(2)} с · людей ${f.people_count} · ${f.status_text}` +
      (f.similarity != null ? ` · сходство ${fmt(f.similarity)}` : "") + (f.passive != null ? ` · живость ${fmt(f.passive)}` : "");
    b.addEventListener("click", () => selectFrame(i));
    tl.append(b);
  });
  let start = 0;
  const bf = fx.best_frame;
  if (bf && bf.index != null && bf.index < data.frames.length) start = bf.index;
  selectFrame(start);
}

function selectFrame(i) {
  const data = state.file;
  if (!data || !data.frames.length) return;
  i = Math.max(0, Math.min(data.frames.length - 1, i));
  state.sel = i;
  document.querySelectorAll("#timeline .tl").forEach((b, j) => b.classList.toggle("sel", j === i));
  const sel = document.querySelectorAll("#timeline .tl")[i];
  if (sel) sel.scrollIntoView({ block: "nearest", inline: "nearest" });
  const f = data.frames[i];
  const s = data.summary || {};
  $("frame-pos").textContent = `кадр ${i + 1} из ${data.frames.length} · ${(f.ts_ms / 1000).toFixed(2)} с`;
  const best = f.best != null ? f.faces[f.best] : null;
  const cls = frameClass(f, s);
  let title = f.status_text;
  if (f.people_count >= 2) title = "В кадре несколько человек. Проходите по одному.";
  else if (f.status === "ok" && f.passive != null && s.passive_threshold != null && f.passive < s.passive_threshold)
    title = "Похоже на подделку (пассивный антиспуфинг)";
  else if (f.status === "ok" && f.similarity != null && s.match_threshold != null)
    title = f.similarity >= s.match_threshold ? "Лицо совпадает с эталоном" : "Лицо не совпадает с эталоном";
  else if (f.status === "ok") title = "Лицо пригодно";
  setStatus($("frame-status"), cls, title, `статус кадра: ${f.status}`);
  fillFacts($("frame-facts"), [
    ["Людей", `${f.people_count} (рамок ${f.people.length})`],
    ["Лиц", f.faces.length],
    ["Сходство", f.similarity != null ? fmt(f.similarity) + (s.match_threshold != null ? ` (порог ${fmt(s.match_threshold)})` : "") : "—"],
    ["«Живость»", f.passive != null ? `${fmt(f.passive)} (порог ${fmt(s.passive_threshold)})` : "—"],
    ["Качество", best ? qualityLine(best.quality) : "—"],
    ["Размер кадра", `${f.width}×${f.height}`],
  ]);
  drawFrame(f, s);
}

function drawFrame(f, s) {
  const canvas = $("frame-canvas");
  const ctx = canvas.getContext("2d");
  if (!f.image) { canvas.width = 640; canvas.height = 360; ctx.clearRect(0, 0, 640, 360); return; }
  const img = new Image();
  img.onload = () => {
    if (state.file && state.file.frames[state.sel] !== f) return; // уже выбран другой кадр
    canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
    ctx.drawImage(img, 0, 0);
    const k = f.image_scale || (img.naturalWidth / f.width);
    const u = Math.max(12, Math.round(Math.min(canvas.width, canvas.height) / 28));
    ctx.font = `600 ${u}px system-ui, sans-serif`;
    ctx.textBaseline = "bottom";
    const label = (text, x, y, color) => {
      const w = ctx.measureText(text).width;
      ctx.fillStyle = "rgba(0,0,0,.7)"; ctx.fillRect(x - 2, y - u - 4, w + 6, u + 6);
      ctx.fillStyle = color; ctx.fillText(text, x + 1, y);
    };
    const rect = (b, color, lw) => {
      ctx.strokeStyle = color; ctx.lineWidth = lw;
      ctx.strokeRect(b[0] * k, b[1] * k, (b[2] - b[0]) * k, (b[3] - b[1]) * k);
    };
    for (const p of f.people) {
      const color = p.in_zone ? "#2fbf5a" : "#9aa3af";
      rect(p.bbox, color, Math.max(2, u / 7));
      label(`${p.in_zone ? "человек" : "фон"} ${fmt(p.score)}`, p.bbox[0] * k + 2, Math.max(u + 4, p.bbox[1] * k - 4), color);
    }
    f.faces.forEach((fc, j) => {
      const isBest = j === f.best;
      let color = fc.quality_status === "ok" ? "#2fbf5a" : "#f0a020";
      if (fc.passive != null && s.passive_threshold != null && fc.passive < s.passive_threshold) color = "#e5484d";
      rect(fc.bbox, color, isBest ? Math.max(3, u / 5) : 2);
      const lines = [[`${QUALITY_SHORT[fc.quality_status] || fc.quality_status} ${fmt(fc.det_score)}`, color]];
      if (fc.passive != null) {
        const live = fc.passive >= (s.passive_threshold ?? 0.5);
        lines.push([`${live ? "живое" : "подделка?"} ${fmt(fc.passive)}`, live ? "#2fbf5a" : "#e5484d"]);
      }
      if (isBest && f.similarity != null) {
        const m = s.match_threshold != null ? (f.similarity >= s.match_threshold) : null;
        lines.push([`сходство ${fmt(f.similarity)}${m === null ? "" : m ? " совпал" : " не совпал"}`,
          m === null ? "#ffffff" : m ? "#2fbf5a" : "#e5484d"]);
      }
      let y = fc.bbox[3] * k + u + 6;
      if (y + lines.length * (u + 6) > canvas.height) y = Math.max(u + 4, fc.bbox[1] * k - 6 - (lines.length - 1) * (u + 6));
      for (const [t, c] of lines) { label(t, fc.bbox[0] * k, y, c); y += u + 6; }
    });
    const n = f.people_count;
    ctx.font = `800 ${u * 2}px system-ui, sans-serif`;
    const nColor = n >= 2 ? "#e5484d" : n === 1 ? "#2fbf5a" : "#ffffff";
    const savedU = u;
    const t = `Людей: ${n}`;
    const w = ctx.measureText(t).width;
    ctx.fillStyle = "rgba(0,0,0,.7)"; ctx.fillRect(savedU - 4, savedU - 2, w + 10, savedU * 2 + 8);
    ctx.fillStyle = nColor; ctx.fillText(t, savedU, savedU * 3 + 2);
  };
  img.src = f.image;
}

$("prev").addEventListener("click", () => selectFrame(state.sel - 1));
$("next").addEventListener("click", () => selectFrame(state.sel + 1));
document.addEventListener("keydown", (e) => {
  if (state.tab !== "file" || !state.file) return;
  if (e.target && /INPUT|SELECT|TEXTAREA/.test(e.target.tagName)) return;
  if (e.key === "ArrowLeft") { selectFrame(state.sel - 1); e.preventDefault(); }
  if (e.key === "ArrowRight") { selectFrame(state.sel + 1); e.preventDefault(); }
});

// ------------------------------------------------------------------ старт

document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
$("camera").addEventListener("change", (e) => { state.camera = e.target.value || null; restartLive(); loadCameras(); });
document.addEventListener("visibilitychange", () => { if (document.hidden) stopLive(); else restartLive(); });

(async function init() {
  let tab = "people";
  try { tab = localStorage.getItem("face-console-tab") || "people"; } catch (_) { /* ignore */ }
  if (!["people", "faces", "file"].includes(tab)) tab = "people";
  await loadCameras();
  setTab(tab);
  loadRef();
  setInterval(loadCameras, 3000);
})();
