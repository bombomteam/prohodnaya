"""Какая рамка ближе к исходному поведению MiniFASNet: исходный RetinaFace против SCRFD/YuNet (как есть и
с пересчётом к виду RetinaFace). Только числа в stdout, изображения и кропы на диск не пишутся.

Запуск в окружении с opencv < 5 (нужен cv2.dnn.readNetFromCaffe для исходного детектора):
  <venv-cv4>/bin/python face/eval/bench/minifasnet/compare_boxes.py \
      --sfas <клон Silent-Face-Anti-Spoofing> --models face/models --labels face/eval/labels.csv \
      [--scrfd-boxes scrfd_boxes.json]   # {путь: [x, y, w, h]} от insightface det_10g, необязательно
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
import sys

import cv2
import numpy as np
from PIL import Image, ImageOps

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from backend import Liveness, to_xywh  # noqa: E402


def retina_net(sfas: pathlib.Path):
    d = sfas / "resources" / "detection_model"
    return cv2.dnn.readNetFromCaffe(str(d / "deploy.prototxt"), str(d / "Widerface-RetinaFace.caffemodel"))


def retina_bbox(net, img):  # Detection.get_bbox исходника
    height, width = img.shape[0], img.shape[1]
    ar = width / height
    if width * height >= 192 * 192:
        img = cv2.resize(img, (int(192 * math.sqrt(ar)), int(192 / math.sqrt(ar))), interpolation=cv2.INTER_LINEAR)
    net.setInput(cv2.dnn.blobFromImage(img, 1, mean=(104, 117, 123)), "data")
    out = net.forward("detection_out").squeeze()
    i = int(np.argmax(out[:, 2]))
    l, t, r, b = out[i, 3] * width, out[i, 4] * height, out[i, 5] * width, out[i, 6] * height
    return [int(l), int(t), int(r - l + 1), int(b - t + 1)]


def yunet_face(models: pathlib.Path, bgr):
    h, w = bgr.shape[:2]
    det = cv2.FaceDetectorYN.create(str(models / "opencv" / "face_detection_yunet_2023mar.onnx"), "", (w, h), 0.6, 0.3, 5000)
    _, faces = det.detect(bgr)
    if faces is None:
        return None
    f = max(faces, key=lambda f: f[2] * f[3])
    return {"bbox": (float(f[0]), float(f[1]), float(f[0] + f[2]), float(f[1] + f[3])), "_row": f}


def iou(a, b):
    ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / (a[2] * a[3] + b[2] * b[3] - inter)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sfas", required=True)
    ap.add_argument("--models", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--scrfd-boxes")
    a = ap.parse_args()
    models = pathlib.Path(a.models)
    lab = pathlib.Path(a.labels)
    net = retina_net(pathlib.Path(a.sfas))
    live = Liveness(models)
    scrfd = {}
    if a.scrfd_boxes:
        for p, (x, y, w, h) in json.load(open(a.scrfd_boxes)).items():
            scrfd[pathlib.Path(p).name] = {"bbox": (x, y, x + w - 1, y + h - 1), "_kps_det": True}
    rows = []
    for r in csv.DictReader(open(lab, encoding="utf-8")):
        p = (lab.parent / r["file"]).resolve()
        grp = {"reference": "ref", "gate": "gate", "badge": "badge"}.get(r["kind"])
        if grp is None:
            continue
        bgr = np.ascontiguousarray(np.asarray(ImageOps.exif_transpose(Image.open(p)).convert("RGB"))[:, :, ::-1])
        rb = retina_bbox(net, bgr)
        pr = live.probs(bgr, rb)
        rec = {"grp": grp, "retina": pr[1] / 2, "retina_argmax": int(np.argmax(pr))}
        yf = yunet_face(models, bgr)
        if yf is not None:
            for mode in ("raw", "retina"):
                bb = to_xywh(yf, mode)
                rec[f"yunet_{mode}"] = live.probs(bgr, bb)[1] / 2
                rec[f"yunet_{mode}_iou"] = iou(rb, bb)
        sf = scrfd.get(p.name)
        if sf is not None:
            for mode in ("raw", "retina"):
                bb = to_xywh(sf, mode)
                rec[f"scrfd_{mode}"] = live.probs(bgr, bb)[1] / 2
                rec[f"scrfd_{mode}_iou"] = iou(rb, bb)
        rows.append(rec)

    print("argmax(sum)==1 при рамке RetinaFace против live>=0.5:",
          sum((r["retina_argmax"] == 1) != (r["retina"] >= 0.5) for r in rows), "расхождений из", len(rows))
    for grp in ("ref", "gate", "badge"):
        sel = [r for r in rows if r["grp"] == grp]
        print(f"\n== {grp} n={len(sel)}  RetinaFace: live>=0.5 {sum(r['retina'] >= 0.5 for r in sel)}, "
              f"argmax==1 {sum(r['retina_argmax'] == 1 for r in sel)}, mean {np.mean([r['retina'] for r in sel]):.3f}")
        for k in ("yunet_raw", "yunet_retina", "scrfd_raw", "scrfd_retina"):
            s = [r for r in sel if k in r]
            if not s:
                continue
            d = np.array([abs(r[k] - r["retina"]) for r in s])
            agree = sum((r[k] >= 0.5) == (r["retina"] >= 0.5) for r in s)
            print(f"  {k:13s} n={len(s):2d} live>=0.5 {sum(r[k] >= 0.5 for r in s):2d}  mean {np.mean([r[k] for r in s]):.3f}"
                  f"  |d| median {np.median(d):.3f} mean {d.mean():.3f} max {d.max():.3f}  "
                  f"решение как у RetinaFace {agree}/{len(s)}  IoU med {np.median([r[k + '_iou'] for r in s]):.2f}")
    allk = ("yunet_raw", "yunet_retina", "scrfd_raw", "scrfd_retina")
    print("\n== все группы")
    for k in allk:
        s = [r for r in rows if k in r]
        if s:
            d = np.array([abs(r[k] - r["retina"]) for r in s])
            agree = sum((r[k] >= 0.5) == (r["retina"] >= 0.5) for r in s)
            print(f"  {k:13s} n={len(s)} |d| mean {d.mean():.3f}  решение как у RetinaFace {agree}/{len(s)}")


if __name__ == "__main__":
    main()
