"""Замер потока камеры: разрешение, кадры в секунду, время декодирования, задержка выборки.

Читает RTSP (например, rtsp://127.0.0.1:8554/live/gate из MediaMTX) через OpenCV/FFmpeg.
Кадры никуда не сохраняет и не показывает — печатает только числа.

Запуск: face/eval/bench/opencv/.venv/bin/python face/tools/camera/probe_stream.py [--url ...] [--seconds 15] [--sample-fps 4]
"""

import argparse
import json
import os
import time
import urllib.request

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
import cv2  # noqa: E402


def pct(v: list[float], p: float) -> float | None:
    if not v:
        return None
    s = sorted(v)
    return round(s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))], 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="rtsp://127.0.0.1:8554/live/gate")
    ap.add_argument("--api", default="http://127.0.0.1:9997/v3/paths/get/live/gate")
    ap.add_argument("--seconds", type=float, default=15)
    ap.add_argument("--sample-fps", type=float, default=4, help="сколько кадров в секунду отдаём на анализ")
    args = ap.parse_args()

    t_open = time.perf_counter()
    cap = cv2.VideoCapture(args.url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise SystemExit(f"не открылся поток {args.url}")
    open_ms = (time.perf_counter() - t_open) * 1000

    read_ms, arrivals, sampled, shapes = [], [], 0, set()
    next_sample, t0, cpu0 = 0.0, time.perf_counter(), time.process_time()
    while time.perf_counter() - t0 < args.seconds:
        t = time.perf_counter()
        ok, frame = cap.read()  # декодирует каждый кадр потока
        if not ok:
            break
        read_ms.append((time.perf_counter() - t) * 1000)
        now = time.perf_counter() - t0
        arrivals.append(now)
        shapes.add(frame.shape[:2])
        if now >= next_sample:  # прореживание до sample-fps — так будет делать модуль
            sampled += 1
            next_sample = now + 1 / args.sample_fps
    cpu_s = time.process_time() - cpu0  # CPU на приём и декодирование всех кадров потока
    cap.release()

    span = arrivals[-1] - arrivals[0] if len(arrivals) > 1 else 0
    gaps = [(b - a) * 1000 for a, b in zip(arrivals, arrivals[1:])]
    out = {
        "url": args.url, "open_ms": round(open_ms), "frames": len(arrivals), "seconds": round(span, 2),
        "fps_stream": round((len(arrivals) - 1) / span, 2) if span else None,
        "frame_hw": sorted(shapes), "read_wait_ms_p50": pct(read_ms, 50), "read_wait_ms_p95": pct(read_ms, 95),
        "gap_ms_p50": pct(gaps, 50), "gap_ms_p95": pct(gaps, 95), "gap_ms_max": round(max(gaps), 1) if gaps else None,
        "sampled_for_analysis": sampled, "sample_fps": args.sample_fps,
        "decode_cpu_cores": round(cpu_s / span, 3) if span else None,  # сколько ядер занято декодированием
    }
    try:
        info = json.load(urllib.request.urlopen(args.api, timeout=3))
        out["source"] = (info.get("source") or {}).get("type")
        out["tracks"] = info.get("tracks")
        out["bytes_received"] = info.get("bytesReceived") or info.get("inboundBytes")
    except OSError as e:
        out["api_error"] = str(e)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
