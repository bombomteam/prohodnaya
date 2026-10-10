"""Перебор параметров правила подсчёта по сохранённым рамкам (dets_<model>.json из debug_people.py)
и по кадрам пакета, уменьшенным на сером поле 1280×720 (как синтетика run_eval). Только числа."""
import json, pathlib, sys
import numpy as np
from PIL import Image

HERE = pathlib.Path(__file__).resolve().parent
FACE = HERE.parents[2]
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(FACE / "eval"))
import backend
from evalkit import imaging as im

VARIANTS = {
    "A rel0.5 cont0.85 edge0.8 (первый вариант)": dict(min_rel_scale=0.5, contain_frac=0.85, edge_rel_height=0.8),
    "B rel0.6 cont0.6 edge0.85 (выбран)": dict(min_rel_scale=0.6, contain_frac=0.6, edge_rel_height=0.85),
    "C rel0.6 cont0.5 edge0.85": dict(min_rel_scale=0.6, contain_frac=0.5, edge_rel_height=0.85),
    "D rel0.65 cont0.6 edge0.85": dict(min_rel_scale=0.65, contain_frac=0.6, edge_rel_height=0.85),
}
paths = sorted((FACE.parent / "case_prerekvizity/Case1_Package1").rglob("*_проходная_*"))
models = sys.argv[1].split(",") if len(sys.argv) > 1 else list(backend.MODELS)
for m in models:
    d = json.load(open(FACE / f"eval/out/people/dets_{m}.json"))
    det = backend.PeopleDetector(FACE / "models", model=m)
    stress = []
    for p in paths:
        img = np.asarray(Image.open(p).convert("RGB"))
        for f in (0.3, 0.5, 0.7):
            x = im.place_scaled(img, f)
            stress.append((x.shape, det.detect(x, score_thr=0.1)))
    print(f"== {m}")
    for name, v in VARIANTS.items():
        P = {**backend.COUNT_PARAMS, **v}
        res = {}
        for kind in ("gate", "synth_two", "synth_gray", "hard1", "hard_far1", "hard_edge50", "hard_edge40"):
            rr = [r for r in d["records"] if r["kind"] == kind]
            ok = sum(backend.count_people(r["dets"], r["shape"], P) == r["expected"] for r in rr)
            res[kind] = f"{ok}/{len(rr)}"
        s2 = sum(backend.count_people(ds, sh, P) >= 2 for sh, ds in stress)
        res["scaled_false2"] = f"{s2}/{len(stress)}"
        print(f"  {name:28s} " + "  ".join(f"{k}={v}" for k, v in res.items()))
