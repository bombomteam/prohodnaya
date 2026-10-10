"""Тестовая страница камеры с айфона: кадры видео и ролики -> сервер, замеры потока.

Только для тестов модуля, только стандартная библиотека Python.
Слушает 127.0.0.1, наружу — через временный HTTPS-туннель (cloudflared).
Доступ только по случайному токену в пути; без токена — 404.

Режимы страницы:
  * поток кадров (3–5 к/с): JPEG по одному через HTTPS POST; сервер считает, сколько кадров
    реально дошло, размер, задержку от съёмки до приёма; по галочке сохраняет кадры как ролик-папку;
  * запись ролика (MediaRecorder) -> файл;
  * одиночный снимок и загрузка файла (запасной вариант «видео из одного кадра»).
Всё сохраняется только в ~/face-data/iphone/, разметка дописывается в ~/face-data/labels.csv.

Запуск:  python3 face/tools/capture/server.py [--port 8765]
"""

import argparse
import csv
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
DATA = Path.home() / "face-data"
RAW = DATA / "iphone"
LABELS = DATA / "labels.csv"
MAX_BYTES = 200 * 1024 * 1024
LABEL_FIELDS = ["file", "subject", "kind", "ref_expected", "gate_expected", "tags", "note", "people"]

# Пресет -> (kind для одиночного снимка, ref_expected, gate_expected, people). Формат face/eval/labels.csv.
# Для потока с сохранением и для роликов kind=clip, ref_expected пусто.
PRESETS = {
    "reference": ("reference", "accept", "", "1"),
    "gate": ("gate", "", "match", "1"),
    "gate_borderline": ("gate", "", "either", "1"),
    "gate_unusable": ("gate", "", "unusable", "1"),
    "screen_photo": ("screen", "any", "spoof", ""),
    "screen_video": ("screen", "any", "spoof", ""),
    "print": ("print", "any", "spoof", ""),
    "two_people": ("multi_person", "reject", "multi_person", "2"),
    "no_person": ("no_person", "reject", "unusable", "0"),
}
TAGS = {"helmet", "hat", "hood", "glasses", "smile", "grimace", "hands", "profile", "back",
        "dark", "backlight", "blur", "small_face", "occluded", "borderline", "wide_angle", "still", "turn_head"}
IMAGE_EXT = {".jpg", ".png", ".webp", ".heic"}
VIDEO_EXT = {".mp4", ".mov", ".webm"}

SESSIONS: dict[str, dict] = {}  # статистика потоков в памяти
LOCK = threading.Lock()


def now_ms() -> float:
    return time.time() * 1000


def sniff_ext(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"msf1"):
            return ".heic"
        return ".mov" if brand == b"qt  " else ".mp4"
    return None


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Ширина и высота JPEG по маркеру SOF (без EXIF-поворота)."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + seg
    return None


def safe(s: str, limit: int = 32) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", s)[:limit]


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))], 1)


def label_row(file: str, q: dict, clip: bool) -> dict | None:
    preset = q.get("preset", "")
    if preset not in PRESETS:
        return None
    kind, ref_exp, gate_exp, people = PRESETS[preset]
    if clip:
        kind, ref_exp = "clip", ""
        gate_exp = gate_exp or "match"
    tags = ";".join(sorted(t for t in q.get("tags", "").split(";") if t in TAGS))
    return {"file": file, "subject": safe(q.get("subject", "")) or "unknown", "kind": kind,
            "ref_expected": ref_exp, "gate_expected": gate_exp, "tags": tags,
            "note": q.get("note", "")[:200].replace("\n", " "), "people": people}


def append_label(row: dict) -> None:
    rows = []
    if LABELS.exists():  # дописываем с переписью заголовка, если колонок стало больше
        with LABELS.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    rows.append(row)
    with LABELS.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LABEL_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def drop_label(file: str) -> None:
    if not LABELS.exists():
        return
    with LABELS.open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["file"] != file]
    with LABELS.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LABEL_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def session_stats(s: dict) -> dict:
    recv = s["recv_ms"]
    span = (recv[-1] - recv[0]) / 1000 if len(recv) > 1 else 0
    gaps = [b - a for a, b in zip(recv, recv[1:])]
    return {
        "frames": len(recv), "seconds": round(span, 2),
        "fps_delivered": round((len(recv) - 1) / span, 2) if span > 0 else None,
        "gap_ms_p50": pct(gaps, 50), "gap_ms_p95": pct(gaps, 95),
        "kb_avg": round(sum(s["bytes"]) / len(s["bytes"]) / 1024, 1) if s["bytes"] else None,
        "latency_ms_p50": pct(s["latency"], 50), "latency_ms_p95": pct(s["latency"], 95),
        "frame_wh": s.get("wh"), "saved": s.get("saved_dir"),
    }


class Handler(BaseHTTPRequestHandler):
    token = ""
    protocol_version = "HTTP/1.1"  # keep-alive: поток кадров идёт по одному соединению

    def log_message(self, fmt, *args):
        path = self.path.split("?")[0].replace(self.token, "<t>")
        if not path.endswith("/frame"):  # кадры не логируем поштучно
            print(f"{datetime.now():%H:%M:%S} {self.command} {path} -> {args[1] if len(args) > 1 else ''}", flush=True)

    def _route(self) -> str | None:
        parts = urlparse(self.path).path.strip("/").split("/")
        if not parts or not secrets.compare_digest(parts[0], self.token):
            return None
        return "/".join(parts[1:])

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = self._route()
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        if route is None:
            return self._json(404, {"error": "not found"})
        if route == "":
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(body)
        if route == "time":  # для оценки сдвига часов клиента
            return self._json(200, {"t": now_ms()})
        if route == "stats":
            with LOCK:
                s = SESSIONS.get(q.get("session", ""))
                return self._json(200, session_stats(s) if s else {"frames": 0})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        route = self._route()
        length = int(self.headers.get("Content-Length") or 0)
        if route is None or length <= 0 or length > MAX_BYTES:
            self.close_connection = True  # тело не читали — соединение не переиспользуем
            return self._json(404 if route is None else 413, {"error": "not found" if route is None else "empty or too large"})
        body = self.rfile.read(length)
        recv = now_ms()
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

        if route == "frame":
            if sniff_ext(body[:16]) != ".jpg":
                return self._json(415, {"error": "not a jpeg"})
            sid = safe(q.get("session", ""), 40)
            if not sid:
                return self._json(400, {"error": "no session"})
            with LOCK:
                s = SESSIONS.setdefault(sid, {"recv_ms": [], "bytes": [], "latency": []})
                if len(SESSIONS) > 50:
                    SESSIONS.pop(next(iter(SESSIONS)))
                s["recv_ms"].append(recv)
                s["bytes"].append(len(body))
                try:
                    s["latency"].append(recv - float(q["t_cap"]))  # t_cap уже в часах сервера
                except (KeyError, ValueError):
                    pass
                s.setdefault("wh", jpeg_size(body))
                n = len(s["recv_ms"])
            if q.get("save") == "1":
                folder = RAW / "sessions" / sid
                if not folder.exists():
                    row = label_row(f"iphone/sessions/{sid}", q, clip=True)
                    if row is None:
                        return self._json(400, {"error": "bad preset"})
                    folder.mkdir(mode=0o700, parents=True)
                    append_label(row)
                    s["saved_dir"] = f"iphone/sessions/{sid}"
                (folder / f"{int(q.get('seq', n)):05d}.jpg").write_bytes(body)
            return self._json(200, {"n": n})

        if route == "stream_end":  # итог потока от клиента -> метаданные рядом с папкой
            try:
                info = json.loads(body)
                sid = safe(info.get("session", ""), 40)
            except ValueError:
                return self._json(400, {"error": "bad json"})
            with LOCK:
                stats = session_stats(SESSIONS[sid]) if sid in SESSIONS else {}
            meta = {"session": sid, "server": stats, "client": info,
                    "user_agent": self.headers.get("User-Agent", ""), "ended": datetime.now().isoformat(timespec="seconds")}
            (RAW / "streams").mkdir(mode=0o700, parents=True, exist_ok=True)
            (RAW / "streams" / f"{sid}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
            return self._json(200, stats)

        if route in ("upload", "clip"):
            ext = sniff_ext(body[:16])
            clip = route == "clip" or ext in VIDEO_EXT
            if ext is None or (clip and ext not in VIDEO_EXT) or (not clip and ext not in IMAGE_EXT):
                return self._json(415, {"error": "unsupported file type"})
            sub = "clips" if clip else "photos"
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
            name = f"{safe(q.get('subject', '')) or 'unknown'}_{safe(q.get('preset', ''))}_{safe(q.get('source', ''), 8)}_{stamp}{ext}"
            row = label_row(f"iphone/{sub}/{name}", q, clip=clip)
            if row is None:
                return self._json(400, {"error": "bad preset"})
            (RAW / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
            (RAW / sub / name).write_bytes(body)
            try:
                client_meta = json.loads(unquote(self.headers.get("X-Capture-Meta", "") or "{}"))
            except ValueError:
                client_meta = {}
            meta = {"file": name, "bytes": len(body), "jpeg_wh": jpeg_size(body) if ext == ".jpg" else None,
                    "received": datetime.now().isoformat(timespec="milliseconds"),
                    "user_agent": self.headers.get("User-Agent", ""), "client": client_meta}
            (RAW / sub / f"{name}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
            append_label(row)
            return self._json(200, {"file": f"{sub}/{name}", "bytes": len(body), "jpeg_wh": meta["jpeg_wh"]})

        if route == "rtt":  # клиент сообщает время загрузки файла
            try:
                info = json.loads(body)
                sub, name = Path(info["file"]).parent.name, Path(info["file"]).name
                meta_path = RAW / safe(sub) / f"{name}.json"
                meta = json.loads(meta_path.read_text())
                meta["client_upload_ms"] = float(info["rtt_ms"])
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1))
            except (ValueError, KeyError, OSError):
                return self._json(400, {"error": "bad rtt"})
            return self._json(200, {"ok": True})

        if route == "delete":  # удалить последнюю неудачную запись (файл или папку потока)
            try:
                rel = json.loads(body)["file"]
            except (ValueError, KeyError):
                return self._json(400, {"error": "bad request"})
            target = (RAW / rel).resolve()
            if RAW.resolve() not in target.parents:
                return self._json(400, {"error": "bad path"})
            if target.is_dir():
                for p in target.glob("*.jpg"):
                    p.unlink()
                target.rmdir()
            else:
                target.unlink(missing_ok=True)
                Path(f"{target}.json").unlink(missing_ok=True)
            drop_label(f"iphone/{rel}")
            return self._json(200, {"deleted": rel})

        return self._json(404, {"error": "not found"})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default=os.environ.get("CAPTURE_TOKEN") or secrets.token_urlsafe(12))
    args = ap.parse_args()
    os.umask(0o077)
    DATA.mkdir(mode=0o700, exist_ok=True)
    Handler.token = args.token
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"capture: http://127.0.0.1:{args.port}/{args.token}/  (данные -> {RAW})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
