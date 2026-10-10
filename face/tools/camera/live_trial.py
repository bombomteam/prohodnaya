"""Живой тест прохода по камере: N подходов человека перед айфоном → сколько пропустил модуль.

Скрипт раз в STEP секунд вызывает POST /v1/verify/camera за последние WINDOW секунд с эталоном
человека и делит поток на «проходы» по паузам, когда в кадре никого нет. Проход засчитан, если
хотя бы одна проверка внутри него вернула ok (как настоящая попытка, когда подтверждение с телефона
пришло, пока человек стоит у камеры). Кадры и векторы не сохраняет, печатает только числа.

Запуск на сервере стенда (face-console слушает 127.0.0.1:8030):
  face/.venv/bin/python face/tools/camera/live_trial.py --ref ~/face-data/iphone/photos/<файл>.jpg \
      --minutes 5 --label "p1 проходы"
"""

import argparse
import collections
import json
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone

BOUNDARY = "----live-trial"


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def post_json(url: str, obj: dict, timeout: float = 30) -> dict:
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def check_reference(base: str, path: str) -> dict:
    data = open(path, "rb").read()
    body = (f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"ref.jpg\"\r\n"
            f"Content-Type: image/jpeg\r\n\r\n").encode() + data + f"\r\n--{BOUNDARY}--\r\n".encode()
    req = urllib.request.Request(base + "/v1/references/check", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8030")
    ap.add_argument("--camera", default="gate-1")
    ap.add_argument("--ref", required=True, help="фото эталона (в ~/face-data)")
    ap.add_argument("--minutes", type=float, default=5)
    ap.add_argument("--window", type=float, default=2.0, help="окно проверки, с")
    ap.add_argument("--step", type=float, default=1.0, help="как часто проверять, с")
    ap.add_argument("--gap", type=float, default=4.0, help="пауза без людей, разделяющая проходы, с")
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    r = check_reference(args.base, args.ref)
    print(f"эталон: {r['reason']['code']} ({r['reason']['text']}); лиц {r.get('faces_count')}, людей {r.get('people_count')}",
          flush=True)
    if not r.get("accepted"):
        sys.exit("эталон не принят — нужен другой снимок")
    ref = {"embedding": r["embedding"], "model_version": r["model_version"], "reference_hash": r["reference_hash"]}

    results = []
    t_end = time.time() + args.minutes * 60
    print(f"старт {datetime.now():%H:%M:%S}, {args.label}; Ctrl+C — закончить раньше", flush=True)
    try:
        while time.time() < t_end:
            t0 = time.time()
            req = {"attempt_id": f"live-{uuid.uuid4().hex[:8]}", "reference": ref, "camera_id": args.camera,
                   "since": iso(t0 - args.window), "until": iso(t0), "wait_ms": 500}
            try:
                j = post_json(args.base + "/v1/verify/camera", req)
            except Exception as exc:  # noqa: BLE001
                print(f"ошибка запроса: {type(exc).__name__}", flush=True)
                time.sleep(args.step)
                continue
            f = j.get("facts") or {}
            live = f.get("liveness") or {}
            present = (f.get("people_max") or 0) > 0 or (f.get("frames_with_face") or 0) > 0
            results.append({"t": t0, "status": j.get("status"), "present": present, "sim": f.get("similarity"),
                            "people": f.get("people_max"), "faces": f.get("faces_max"),
                            "passive": live.get("passive"), "seq": live.get("sequence"),
                            "frames": f.get("frames_total")})
            mark = "·" if not present else ("✓" if j.get("status") == "ok" else "✗")
            sim = f.get("similarity")
            print(f"{datetime.now():%H:%M:%S} {mark} {j.get('status'):18s} кадров {f.get('frames_total')} "
                  f"людей {f.get('people_max')} лиц {f.get('faces_max')} сходство {sim if sim is None else round(sim, 3)} "
                  f"живость {live.get('passive')} / {live.get('sequence')}", flush=True)
            time.sleep(max(0.0, args.step - (time.time() - t0)))
    except KeyboardInterrupt:
        pass

    # Проходы: подряд идущие проверки с человеком в кадре; пауза без людей ≥ gap — граница.
    passes, cur, last_present = [], [], None
    for r in results:
        if r["present"]:
            if cur and last_present is not None and r["t"] - last_present > args.gap:
                passes.append(cur)
                cur = []
            cur.append(r)
            last_present = r["t"]
    if cur:
        passes.append(cur)

    print(f"\n=== {args.label}: проходов {len(passes)} ===")
    accepted = 0
    for i, p in enumerate(passes, 1):
        ok = any(r["status"] == "ok" for r in p)
        accepted += ok
        reasons = collections.Counter(r["status"] for r in p)
        sims = [r["sim"] for r in p if r["sim"] is not None]
        print(f"проход {i:2d}: {'ПУСТИЛ' if ok else 'отказ'}; {len(p)} проверок; причины {dict(reasons)}; "
              f"сходство макс {max(sims):.3f}" if sims else
              f"проход {i:2d}: {'ПУСТИЛ' if ok else 'отказ'}; {len(p)} проверок; причины {dict(reasons)}")
    print(f"ИТОГО: пустил {accepted} из {len(passes)}")


if __name__ == "__main__":
    main()
