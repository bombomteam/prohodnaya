#!/usr/bin/env python3
"""Сводная таблица по нескольким прогонам: python face/eval/compare.py face/eval/out/*/summary.json

Печатает markdown в stdout: строки — метрики, столбцы — прогоны. Только числа из summary.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def g(d, *path, default=None):
    for p in path:
        if not isinstance(d, dict) or p not in d or d[p] is None:
            return default
        d = d[p]
    return d


def of(k, n) -> str:
    return "—" if k is None or not n else f"{k}/{n}"


def num(x, nd=3) -> str:
    if x is None:
        return "—"
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def by_exp(s, exp, key, section="gate"):
    return g(s, section, "by_expected", exp, key)


ROWS = [
    ("Модель", lambda s: f"{g(s, 'run', 'backend', 'name')} {g(s, 'run', 'backend', 'model_version')}"),
    ("Живость по кадру", lambda s: g(s, "run", "liveness", "name", default="—")),
    ("Подсчёт людей", lambda s: g(s, "run", "people", "name", default="—")),
    ("Живость по ролику", lambda s: g(s, "run", "seq_liveness", "name", default="—")),
    ("Порог сходства", lambda s: num(g(s, "run", "threshold_used"))),
    ("**Свои кадры match: совпало**", lambda s: of(by_exp(s, "match", "matched"), by_exp(s, "match", "n"))),
    ("Свои пары (качество ок): принято", lambda s: of(g(s, "pairs", "genuine", "accepted"), g(s, "pairs", "genuine", "n"))),
    ("**Чужие пары: ложных совпадений**", lambda s: of(g(s, "pairs", "impostor", "false_accepts"), g(s, "pairs", "impostor", "n"))),
    ("**Похожие (Кимы): ложных**", lambda s: of(g(s, "pairs", "similar", "false_accepts"), g(s, "pairs", "similar", "n"))),
    ("Эталон↔эталон: ложных", lambda s: of(g(s, "pairs", "ref_ref", "false_accepts"), g(s, "pairs", "ref_ref", "n"))),
    ("Мин. своих / макс. чужих", lambda s: f"{num(g(s, 'pairs', 'genuine', 'dist', 'min'))} / {num(g(s, 'pairs', 'impostor', 'dist', 'max'))}"),
    ("Макс. похожих", lambda s: num(g(s, "pairs", "similar", "max"))),
    ("Зазор своих и чужих", lambda s: num(g(s, "pairs", "gap"))),
    ("Ноль ложных при пороге >", lambda s: num(g(s, "pairs", "zero_fa", "impostor_max"))),
    ("Своих выше макс. чужого", lambda s: of(g(s, "pairs", "zero_fa", "genuine_accepted_above_max"), g(s, "pairs", "zero_fa", "genuine_n"))),
    ("Непригодные: понятная причина", lambda s: of(by_exp(s, "unusable", "quality_reason"), by_exp(s, "unusable", "n"))),
    ("Непригодные/пограничные: «не совпадает»", lambda s: str((by_exp(s, "unusable", "mismatch") or 0) + (by_exp(s, "either", "mismatch") or 0))),
    ("**Кадры, совпавшие с чужим (критично)**", lambda s: str(sum((by_exp(s, e, "matched_other") or 0) for e in ("match", "either", "unusable")))),
    ("Эталоны пакета приняты", lambda s: of(g(s, "references", "by_expected", "accept", "accepted"), g(s, "references", "by_expected", "accept", "n"))),
    ("Плохие эталоны (синтетика) отклонены", lambda s: of(g(s, "references", "synthetic", "rejected"), g(s, "references", "synthetic", "total"))),
    ("Подделки как кадр: пропущено", lambda s: of(g(s, "spoofs", "accepted"), g(s, "spoofs", "n"))),
    ("Подмена эталоном: зазор сходства", lambda s: num(g(s, "replay_reference", "sim_gap"))),
    ("Подмена эталоном: зазор pHash лица, бит", lambda s: num(g(s, "replay_reference", "hash", "face_p", "gap"))),
    ("Повтор кадра: зазор сходства", lambda s: num(g(s, "replay_frame", "sim_gap"))),
    ("Повтор кадра: зазор dHash кадра, бит", lambda s: num(g(s, "replay_frame", "hash", "img_d", "gap"))),
    ("Живость: живых отклонено (BPCER)", lambda s: of(g(s, "liveness", "live", "rejected"), g(s, "liveness", "live", "n"))),
    ("Живость: подделок пропущено (APCER)", lambda s: of(g(s, "liveness", "spoof", "accepted"), g(s, "liveness", "spoof", "n"))),
    ("Люди: ровно 1 из ожидаемых 1", lambda s: of(g(s, "people", "expected_one", "exact_one"), g(s, "people", "expected_one", "n"))),
    ("Люди: синтетика точно", lambda s: of(g(s, "people", "synthetic", "exact"), g(s, "people", "synthetic", "n"))),
    ("Ролики match: совпало", lambda s: of(g(s, "clips", "gate", "by_expected", "match", "matched"), g(s, "clips", "gate", "by_expected", "match", "n"))),
    ("Ролики: ложных чужих", lambda s: of(g(s, "clips", "pairs", "impostor", "false_accepts"), g(s, "clips", "pairs", "impostor", "n"))),
    ("Живость по ролику: статичных пропущено", lambda s: of(g(s, "seq_liveness", "static_synthetic", "accepted_as_live"), g(s, "seq_liveness", "static_synthetic", "n"))),
    ("Время кадра, мс: медиана / p95", lambda s: f"{num(g(s, 'timing', 'stages', 'total', 'median_ms'), 1)} / {num(g(s, 'timing', 'stages', 'total', 'p95_ms'), 1)}"),
    ("Кадров/с в один поток", lambda s: num(g(s, "timing", "fps_single_stream"), 1)),
]


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    runs = []
    for p in argv:
        try:
            s = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"пропущен {p}: {type(exc).__name__}", file=sys.stderr)
            continue
        runs.append((g(s, "run", "name", default=Path(p).parent.name), s))
    if not runs:
        return 1
    print("| метрика | " + " | ".join(name for name, _ in runs) + " |")
    print("|---|" + "---|" * len(runs))
    for title, fn in ROWS:
        cells = []
        for _, s in runs:
            try:
                cells.append(fn(s))
            except Exception:
                cells.append("—")
        print(f"| {title} | " + " | ".join(cells) + " |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
