"""Отладочный прогон детектора людей (предварительные цифры; итог — face/eval/run_eval.py --people).

Пакет кейса + синтетика в памяти (на диск картинки не пишутся):
  * collage2 — два разных кадра проходной рядом → ожидание 2;
  * gray1    — кадр, уменьшенный вдвое, посередине серого поля → ожидание 1.
Плюс ~/face-data/labels.csv, если есть: только числа, картинки не показываются.

Запуск:
  face/eval/bench/people/.venv/bin/python face/eval/bench/people/debug_people.py [--models yolox_tiny,yolox_m]
Сырые рамки — в face/eval/out/people/dets_<model>.json (папка в .gitignore).
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys
import time

import numpy as np
from PIL import Image, ImageOps

HERE = pathlib.Path(__file__).resolve().parent
FACE = HERE.parents[2]
REPO = FACE.parent
PKG = REPO / "case_prerekvizity/Case1_Package1/Первый пакет/Персонал"
OUT = FACE / "eval/out/people"
FACE_DATA = pathlib.Path.home() / "face-data"

sys.path.insert(0, str(HERE))
import backend  # noqa: E402

LOW_THR = 0.10  # сохраняем всё от 0.10, чтобы потом перебирать порог без повторного прогона


def load_rgb(path: pathlib.Path) -> np.ndarray:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        return np.asarray(im.convert("RGB"))


def package_items():
    items = []
    for p in sorted(PKG.rglob("*")):
        if not p.is_file():
            continue
        stem = p.stem
        if "_проходная_" in stem:
            kind = "gate"
        elif stem.endswith("_фото"):
            kind = "ref"
        elif stem.endswith("_пропуск"):
            kind = "pass"
        else:
            continue
        items.append((p, kind))
    return items


def synth(gate_paths):
    """Синтетика в памяти: пары соседних кадров разных людей и уменьшенный кадр на сером поле."""
    out = []
    by_person = {}
    for p in gate_paths:
        by_person.setdefault(p.parent.name, []).append(p)
    persons = sorted(by_person)
    for i, a in enumerate(persons):
        b = persons[(i + 1) % len(persons)]
        pa, pb = by_person[a][0], by_person[b][-1]
        ia, ib = load_rgb(pa), load_rgb(pb)
        h = min(ia.shape[0], ib.shape[0])
        ia = np.asarray(Image.fromarray(ia).resize((ia.shape[1] * h // ia.shape[0], h)))
        ib = np.asarray(Image.fromarray(ib).resize((ib.shape[1] * h // ib.shape[0], h)))
        out.append((f"collage2:{pa.name}+{pb.name}", "synth_two", 2, np.concatenate([ia, ib], axis=1)))
    for p in gate_paths[::3]:
        im = load_rgb(p)
        h, w = im.shape[:2]
        small = np.asarray(Image.fromarray(im).resize((w // 2, h // 2)))
        canvas = np.full((h, w, 3), 128, np.uint8)
        y0, x0 = (h - h // 2) // 2, (w - w // 2) // 2
        canvas[y0 : y0 + h // 2, x0 : x0 + w // 2] = small
        out.append((f"gray1:{p.name}", "synth_gray", 1, canvas))
    return out


def hard_synth(gate_paths):
    """Тяжёлые условия (в памяти). Ожидание: 1, кроме near2 (второй рядом, 0,75 масштаба) → 2."""
    from PIL import ImageFilter
    import io

    rng = np.random.default_rng(0)
    out = []
    for i, p in enumerate(gate_paths[::2]):
        im = load_rgb(p)
        h, w = im.shape[:2]
        pil = Image.fromarray(im)
        out.append((f"dark:{p.name}", "hard1", 1, (im.astype(np.float32) * 0.18).astype(np.uint8)))
        out.append((f"blur:{p.name}", "hard1", 1, np.asarray(pil.filter(ImageFilter.GaussianBlur(8)))))
        out.append((f"lowres:{p.name}", "hard1", 1, np.asarray(pil.resize((w // 6, h // 6)).resize((w, h)))))
        buf = io.BytesIO(); pil.save(buf, "JPEG", quality=10); buf.seek(0)
        out.append((f"jpeg10:{p.name}", "hard1", 1, np.asarray(Image.open(buf).convert("RGB"))))
        noisy = np.clip(im.astype(np.float32) + rng.normal(0, 25, im.shape), 0, 255).astype(np.uint8)
        out.append((f"noise:{p.name}", "hard1", 1, noisy))
        # очень близко: только голова и плечи (верхние 55% кадра, центральные 80%)
        out.append((f"close:{p.name}", "hard1", 1, np.ascontiguousarray(im[: int(h * 0.55), int(w * 0.1) : int(w * 0.9)])))
        # 16:9 из центра кадра (как 1280×720 с близко стоящим человеком)
        hh = w * 9 // 16
        out.append((f"wide169:{p.name}", "hard1", 1, np.ascontiguousarray(im[(h - hh) // 3 : (h - hh) // 3 + hh])))
        # второй человек справа, масштаб s, по низу кадра
        q = gate_paths[(gate_paths.index(p) + 7) % len(gate_paths)]
        sec = load_rgb(q)
        for sc, exp, tag in ((0.35, 1, "far1"),):
            sw, sh = int(w * sc), int(h * sc)
            canvas = np.full((h, w + sw, 3), 128, np.uint8)
            canvas[:, :w] = im
            canvas[h - sh :, w:] = np.asarray(Image.fromarray(sec).resize((sw, sh)))
            out.append((f"{tag}:{p.name}+{q.name}", "hard_" + tag, exp, canvas))
        # второй человек рядом (тот же масштаб), но обрезан правым краем кадра: видна только левая доля
        # vis его кадра (человек в кадре пакета по центру, поэтому vis=0,5 ≈ половина тела, 0,4 ≈ треть)
        sw, sh = w, h
        sec_s = sec
        for vis in (0.5, 0.4):
            vw = int(sw * vis)
            canvas = np.full((h, w + vw, 3), 128, np.uint8)
            canvas[:, :w] = im
            canvas[h - sh :, w:] = sec_s[:, :vw]
            out.append((f"edge{int(vis*100)}:{p.name}+{q.name}", f"hard_edge{int(vis*100)}", 2, canvas))
    return out


def face_data_items():
    lab = FACE_DATA / "labels.csv"
    if not lab.is_file():
        return []
    items = []
    with lab.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            path = FACE_DATA / row["file"]
            exp = None
            if (row.get("people") or "").strip().isdigit():
                exp = int(row["people"])
            elif row.get("gate_expected") == "multi_person":
                exp = 2
            items.append((path, row.get("kind", ""), exp, row))
    return items


def iter_clip_frames(path: pathlib.Path, step_s=0.25, max_frames=40):
    """Кадры ролика (≈4 кадра/с) или файлы из папки кадров. Только в память."""
    import cv2

    if path.is_dir():
        files = sorted(x for x in path.iterdir() if x.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
        for x in files[:max_frames]:
            yield load_rgb(x)
        return
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    step = max(1, int(round(fps * step_s)))
    i = n = 0
    while n < max_frames:
        ok, fr = cap.read()
        if not ok:
            break
        if i % step == 0:
            yield fr[:, :, ::-1].copy()  # cv2 уже применяет поворот из метаданных
            n += 1
        i += 1
    cap.release()


def timing(det, sizes=((1280, 720), (1920, 1080)), n=30):
    src = load_rgb(next(PKG.rglob("*_проходная_1.jpg")))
    res = {}
    for w, h in sizes:
        img = np.asarray(Image.fromarray(src).resize((w, h)))
        for _ in range(3):
            det.detect(img)
        ts = []
        for _ in range(n):
            t = time.perf_counter()
            det.detect(img)
            ts.append((time.perf_counter() - t) * 1000)
        res[f"{w}x{h}"] = (float(np.median(ts)), float(np.percentile(ts, 95)))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(backend.MODELS))
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--no-timing", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    pkg = package_items()
    gate_paths = [p for p, k in pkg if k == "gate"]
    syn = synth(gate_paths) + hard_synth(gate_paths)
    fd = face_data_items()
    models_dir = FACE / "models"

    for key in args.models.split(","):
        det = backend.PeopleDetector(models_dir, model=key, threads=args.threads)
        rec = []
        for p, kind in pkg:
            img = load_rgb(p)
            d = det.detect(img, score_thr=LOW_THR)
            rec.append({"id": str(p.relative_to(PKG)), "kind": kind, "expected": 1 if kind == "gate" else None, "shape": img.shape, "dets": d})
        for name, kind, exp, img in syn:
            d = det.detect(img, score_thr=LOW_THR)
            rec.append({"id": name, "kind": kind, "expected": exp, "shape": img.shape, "dets": d})
        for path, kind, exp, row in fd:
            if not path.exists():
                continue
            # Идентификатор — только номер строки: имена файлов могут содержать имена людей.
            rid = f"face-data#{fd.index((path, kind, exp, row))}"
            try:
                if kind == "clip":
                    frames = [(fr.shape, det.detect(fr, score_thr=LOW_THR)) for fr in iter_clip_frames(path)]
                    rec.append({"id": rid, "kind": "clip", "expected": exp, "tags": row.get("tags", ""), "frames": frames})
                else:
                    img = load_rgb(path)
                    rec.append({"id": rid, "kind": kind, "expected": exp, "tags": row.get("tags", ""), "shape": img.shape, "dets": det.detect(img, score_thr=LOW_THR)})
            except Exception as e:  # битый файл не должен ронять прогон
                rec.append({"id": rid, "kind": kind, "expected": exp, "error": type(e).__name__})
        t = {} if args.no_timing else timing(det)
        (OUT / f"dets_{key}.json").write_text(json.dumps({"model": key, "version": det.model_version, "threads": args.threads, "timing_ms": t, "records": rec}, ensure_ascii=False))
        summarize(key, rec, t, models_dir / "people" / backend.MODELS[key]["file"])


def summarize(key, rec, t, wpath, params=None):
    p = {**backend.COUNT_PARAMS, **(params or {})}
    def cnt(r):
        return backend.count_people(r["dets"], r["shape"], p)
    gate = [r for r in rec if r["kind"] == "gate"]
    g = [cnt(r) for r in gate]
    s2 = [cnt(r) for r in rec if r["kind"] == "synth_two"]
    s1 = [cnt(r) for r in rec if r["kind"] == "synth_gray"]
    raw = [sum(d["score"] >= p["score_thr"] for d in r["dets"]) for r in rec if "dets" in r and r["kind"] in ("gate", "hard1", "synth_gray")]
    print(f"== {key}  weights {wpath.stat().st_size/1e6:.1f} MB  params={p}")
    print(f"  gate frames: {len(g)}  ==1: {g.count(1)}  ==0: {g.count(0)}  >=2: {sum(x >= 2 for x in g)}")
    bad = [(r["id"], c) for r, c in zip(gate, g) if c != 1]
    for i, c in bad:
        print(f"    !! {c}  {i}")
    print(f"  synth two (expect 2): {s2.count(2)}/{len(s2)}  got {s2}")
    print(f"  synth gray (expect 1): {s1.count(1)}/{len(s1)}  got {s1}")
    print(f"  raw boxes>=thr without rule on expected-1 images: >=2 in {sum(x >= 2 for x in raw)}/{len(raw)}")
    for kind in ("hard1", "hard_far1", "hard_edge50", "hard_edge40"):
        rr = [r for r in rec if r["kind"] == kind]
        c = [cnt(r) for r in rr]
        exp = rr[0]["expected"] if rr else None
        wrong = [(r["id"].split(":")[0], x) for r, x in zip(rr, c) if x != exp]
        from collections import Counter
        print(f"  {kind} (expect {exp}): ok {sum(x == exp for x in c)}/{len(c)}  wrong by type: {dict(Counter(wrong))}")
        if kind.startswith("hard_edge"):
            # детектор сам по себе: нашёл ли он обрезанного второго (рамка ≥ порога правее склейки)
            found = sum(any(d["score"] >= p["score_thr"] and d["bbox"][2] > r["shape"][1] - 5 and d["bbox"][0] > r["shape"][0] - 100 for d in r["dets"]) for r in rr)
            print(f"      detector found the cut second person (score>={p['score_thr']}): {found}/{len(rr)}")
    for kind in ("ref", "pass"):
        c = [cnt(r) for r in rec if r["kind"] == kind]
        print(f"  {kind}: counts {c}")
    fdr = [r for r in rec if r["id"].startswith("face-data#")]
    if fdr:
        print(f"  face-data: {len(fdr)} items")
        for r in fdr:
            if "error" in r:
                print(f"    {r['id']} kind={r['kind']} error={r['error']}")
            elif r["kind"] == "clip":
                cs = [backend.count_people(d, s, p) for s, d in r["frames"]]
                print(f"    {r['id']} clip exp={r['expected']} tags={r['tags']!r} frames={len(cs)} max={max(cs) if cs else None} share>=2={np.mean([c >= 2 for c in cs]) if cs else 0:.2f} counts={cs}")
            else:
                print(f"    {r['id']} {r['kind']} exp={r['expected']} tags={r['tags']!r} count={cnt(r)}")
    for k, (med, p95) in t.items():
        print(f"  time {k}: median {med:.1f} ms, p95 {p95:.1f} ms")


if __name__ == "__main__":
    main()
