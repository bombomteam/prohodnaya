"""report.md из summary.json: только числа, статусы и отображаемые имена."""
from __future__ import annotations

from pathlib import PurePosixPath

from .quality import REASON_TEXT

EXPECT_TEXT = {
    "match": "свой пригодный кадр (match)",
    "unusable": "непригодный кадр (unusable)",
    "either": "пограничный кадр (either)",
    "spoof": "подделка (spoof)",
    "multi_person": "двое и больше в кадре (multi_person)",
    "disputed": "спорный кадр (disputed)",
}


def _f(x, nd: int = 3) -> str:
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _short(path: str) -> str:
    return PurePosixPath(path).name or path


def table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_f(c) if not isinstance(c, str) else c for c in r) + " |")
    return "\n".join(out)


def _of(k, n) -> str:
    return f"{k} из {n}" if n else "—"


def _adapter(d: dict | None) -> str:
    if not d:
        return "не задан"
    return f"{d['name']} {d['model_version']} (порог {d['threshold_used']:g}, по умолчанию {d['threshold_default']:g})"


def _matrix(matrix: dict, statuses: list[str], row_label) -> str:
    used = [s for s in statuses if any(s in v for v in matrix.values())]
    rows = []
    for exp, counts in matrix.items():
        rows.append([row_label(exp)] + [str(counts.get(s, 0)) for s in used] + [str(sum(counts.values()))])
    return table(["ожидание"] + used + ["всего"], rows)


def _gate_block(gate: dict, title_prefix: str = "", threshold: float | None = None) -> list[str]:
    L = []
    L.append(_matrix(gate["matrix"], gate["statuses"], lambda e: EXPECT_TEXT.get(e, e)))
    L.append("")
    b = gate["by_expected"]
    if "match" in b:
        s = b["match"]
        L.append(f"- **{title_prefix}Свои пригодные кадры:** совпало **{s['matched']} из {s['n']}**; "
                 f"отказ по качеству {s['quality_reason']}, «не совпадает» {s['mismatch']}, "
                 f"«подделка» {s['spoof']}, «несколько человек» {s['multiple_people']}, "
                 f"нет эталона {s['no_reference']}, ошибка {s['error']}; "
                 f"совпали с чужим эталоном (критично) **{s['matched_other']}**.")
    if "unusable" in b:
        s = b["unusable"]
        L.append(f"- **{title_prefix}Непригодные кадры:** понятная причина **{s['quality_reason']} из {s['n']}**; "
                 f"«не совпадает» (ошибка причины) **{s['mismatch']}**; совпали со своим (допустимо) {s['matched']}; "
                 f"«подделка» {s['spoof']}, «несколько человек» {s['multiple_people']}; "
                 f"совпали с чужим (критично) **{s['matched_other']}**.")
    if "either" in b:
        s = b["either"]
        L.append(f"- **{title_prefix}Пограничные кадры:** допустимый ответ без совпадения с чужим {s['ok']} из {s['n']} "
                 f"(совпало {s['matched']}, причина качества {s['quality_reason']}); "
                 f"«не совпадает» **{s['mismatch']}**; совпали с чужим (критично) **{s['matched_other']}**.")
    if "spoof" in b:
        s = b["spoof"]
        L.append(f"- **{title_prefix}Подделки как кадр:** отклонено {s['n'] - s['matched']} из {s['n']} "
                 f"(как «подделка» {s['spoof']}); **пропущено как совпадение {s['matched']}**.")
    if "multi_person" in b:
        s = b["multi_person"]
        L.append(f"- **{title_prefix}Двое в кадре:** отказ «по одному» {s['ok']} из {s['n']}; "
                 f"пропущено как совпадение **{s['critical']}**.")
    if gate["errors"]:
        L.append("")
        L.append(f"{title_prefix}Кадры с неверным или критичным результатом:")
        L.append("")
        L.append(table(["файл", "человек", "ожидание", "статус", "пояснение", "сходство со своим",
                        "макс. с чужим (кто)", "теги"],
                       [[_short(e["file"]), e["subject"], e["expected"], e["status"], e["reason"] or "",
                         e["sim_own"], f"{_f(e['max_other_sim'])} ({e['max_other_subject'] or '—'})",
                         e["tags"]] for e in gate["errors"]]))
    if gate.get("disputed"):
        def ge(x):
            return "—" if x is None or threshold is None else ("да" if x >= threshold else "нет")
        L.append("")
        L.append(f"{title_prefix}Спорные кадры (`disputed`, {len(gate['disputed'])}): тот же ли человек или живой ли "
                 f"кадр — по виду не решить. Не входят ни в успехи, ни в ошибки, ни в пары и сетку порогов; "
                 f"порог — {_f(threshold)}.")
        L.append("")
        L.append(table(["файл", "человек", "статус", "пояснение", "сходство со своим", "≥ порога",
                        "макс. с чужим (кто)", "чужой ≥ порога", "liveness", "теги"],
                       [[_short(e["file"]), e["subject"], e["status"], e["reason"] or "", e["sim_own"],
                         ge(e["sim_own"]), f"{_f(e['max_other_sim'])} ({e['max_other_subject'] or '—'})",
                         ge(e["max_other_sim"]), e.get("liveness"), e["tags"]] for e in gate["disputed"]]))
    return L


def _pairs_block(p: dict) -> list[str]:
    L = []
    g, i, s, rr = p["genuine"], p["impostor"], p["similar"], p["ref_ref"]
    t = p["threshold"]
    L.append(f"На пороге **{_f(t)}**:")
    L.append("")
    L.append(f"- свои пары (пригодный кадр `match` ↔ свой эталон): принято **{g['accepted']} из {g['n']}**, "
             f"отклонено {g['rejected']};")
    for exp, d in p["genuine_by_expected"].items():
        if exp != "match" and d["n"]:
            L.append(f"  - кадры `{exp}`, прошедшие качество: принято {d['accepted']} из {d['n']};")
    L.append(f"- чужие пары (кадр, прошедший качество ↔ чужой эталон): ложных совпадений "
             f"**{i['false_accepts']} из {i['n']}**;")
    L.append(f"- похожие люди (пары из `similar_pairs`, обе стороны): ложных **{s['false_accepts']} из {s['n']}**;")
    L.append(f"- эталон ↔ эталон: ложных {rr['false_accepts']} из {rr['n']} "
             f"(макс. {_f(rr['max'])}; похожие: макс. {_f(rr['similar_max'])}).")
    L.append("")
    gd, idist = g["dist"] or {}, i["dist"] or {}
    L.append(table(["распределение", "n", "мин", "p05", "медиана", "p95", "макс"], [
        ["свои (match, качество ок)", gd.get("n"), gd.get("min"), gd.get("p05"), gd.get("median"),
         gd.get("p95"), gd.get("max")],
        ["свои (все кадры с лицом)", *(lambda d: [d.get("n"), d.get("min"), d.get("p05"), d.get("median"),
                                                  d.get("p95"), d.get("max")])(p["genuine_all_faces"]["dist"] or {})],
        ["чужие (качество ок)", idist.get("n"), idist.get("min"), idist.get("p05"), idist.get("median"),
         idist.get("p95"), idist.get("max")],
    ]))
    L.append("")
    L.append(f"Зазор «мин. своих − макс. чужих»: **{_f(p['gap'])}** "
             f"(макс. похожих {_f(s['max'])}). Положительный зазор — пороги между ними разделяют без ошибок.")
    z = p["zero_fa"]
    if z.get("impostor_max") is not None:
        L.append("")
        L.append(f"Ноль ложных совпадений чужих: при пороге **> {_f(z['impostor_max'])}** "
                 f"(с учётом пар эталон↔эталон — > {_f(z['with_ref_ref_max'])}). "
                 f"Своих при этом выше макс. чужого: **{z['genuine_accepted_above_max']} из {z['genuine_n']}**. "
                 f"Первый порог сетки без ложных: {_f(z.get('t_min_grid'))}, своих на нём "
                 f"{_f(z.get('genuine_accepted_at_t_min_grid'))}. Порог скрипт не выбирает.")
    if p["grid"]:
        L.append("")
        L.append("<details><summary>Сетка порогов</summary>")
        L.append("")
        L.append(table(["порог", "свои match принято", "все свои (кач. ок) принято", "чужие ложно",
                        "похожие ложно", "эталон↔эталон ложно"],
                       [[f"{r['t']:.2f}", f"{r['genuine_accepted']}/{r['genuine_n']}",
                         f"{r['genuine_ok_any_accepted']}/{r['genuine_ok_any_n']}", str(r["impostor_fa"]),
                         str(r["similar_fa"]), str(r["ref_ref_fa"])] for r in p["grid"]]))
        L.append("")
        L.append("</details>")
    if p["hardest_impostors"]:
        L.append("")
        L.append("Самые похожие чужие пары (кадр человека → эталон другого):")
        L.append("")
        L.append(table(["кадр чей", "эталон чей", "макс. сходство"],
                       [[h["probe_subject"], h["ref_subject"], h["max_score"]] for h in p["hardest_impostors"]]))
    return L


def _replay_block(r: dict, honest_title: str) -> list[str]:
    L = []
    rows = []
    for name, v in r["variants"].items():
        rows.append([name, f"{v['detected']}/{v['n']}", v["sim_min"], v["sim_median"],
                     ", ".join(f"{k} {n}" for k, n in sorted(v["statuses"].items())),
                     _f(v["img_d_max"]), _f(v["img_p_max"]), _f(v["face_d_max"]), _f(v["face_p_max"])])
    L.append(table(["вариант", "лицо найдено", "сходство мин", "медиана", "статус как кадр",
                    "dHash кадра макс", "pHash кадра макс", "dHash лица макс", "pHash лица макс"], rows))
    L.append("")
    L.append(f"- Сходство: мин. у подмены (без «как есть») **{_f(r['replay_sim_min'])}**, "
             f"макс. у {honest_title} **{_f(r['honest_sim_max'])}** (n={r['honest_n']}); "
             f"зазор **{_f(r['sim_gap'])}** (> 0 — правило «слишком похоже» отделяет подмену).")
    for h, title in (("img_d", "dHash кадра"), ("img_p", "pHash кадра"), ("face_d", "dHash лица"),
                     ("face_p", "pHash лица")):
        d = r["hash"][h]
        L.append(f"- {title}: макс. расстояние у подмены {_f(d['replay_max'])}, "
                 f"мин. у честных {_f(d['honest_min'])} (n={d['honest_n']}), зазор **{_f(d['gap'])}** бит.")
    return L


def render_report(s: dict) -> str:
    run = s["run"]
    L: list[str] = []
    b = run["backend"]
    L.append(f"# Прогон «{run['name']}»: {b['name']} {b['model_version']}")
    L.append("")
    L.append(f"Создан {run['created_utc']}, длительность {run['duration_s']} с. "
             f"Python {run['python']}, numpy {run['numpy']}, Pillow {run['pillow']}.")
    L.append("")
    L.append(f"- Порог сходства: **{run['threshold_used']:g}** "
             f"({'из --threshold' if run['threshold_source'] == 'cli' else 'по умолчанию адаптера'}).")
    L.append(f"- Живость по кадру: {_adapter(run['liveness'])}.")
    L.append(f"- Подсчёт людей: {_adapter(run['people'])}.")
    L.append(f"- Живость по ролику: {_adapter(run['seq_liveness'])}.")
    labels = ", ".join((src["path"] or f"приватный источник L{src['index']}") + f" ({src['rows']} строк)"
                       for src in run["labels"])
    L.append(f"- Разметка: {labels}.")
    if run["similar_pairs"]:
        L.append("- Похожие люди: " + "; ".join(" ↔ ".join(p) for p in run["similar_pairs"]) + ".")
    c = s["counts"]
    L.append(f"- Строк: {c['rows']}; по видам: " + ", ".join(f"{k} {v}" for k, v in c["by_kind"].items())
             + f"; ошибок чтения/обработки: {c['errors']}; эталонов заведено: {c['enrolled_references']}.")
    if c["subjects_without_reference"]:
        L.append(f"- Без пригодного эталона: {', '.join(c['subjects_without_reference'])}.")
    L.append("")
    L.append("<details><summary>Пороги качества, с которыми считали</summary>")
    L.append("")
    q = run["quality"]
    keys = sorted(set(q["ref"]) | set(q["gate"]))
    L.append(table(["ключ", "эталон (ref)", "кадр (gate)"], [[k, _f(q["ref"].get(k), 2), _f(q["gate"].get(k), 2)]
                                                               for k in keys]))
    L.append("")
    L.append("Общие: " + ", ".join(f"{k}={v:g}" for k, v in q["common"].items())
             + (f". Переопределено: {', '.join(run['quality_overrides'])}" if run["quality_overrides"] else ""))
    L.append("")
    L.append("</details>")

    # 2. эталоны
    r = s["references"]
    L += ["", "## Эталоны: проверка пригодности", ""]
    rows = []
    for exp, d in r["by_expected"].items():
        rows.append([exp, str(d["n"]), str(d["accepted"]), str(d["rejected"]),
                     ", ".join(f"{k} {v}" for k, v in sorted(d["statuses"].items()))])
    L.append(table(["ожидание", "n", "принято", "отклонено", "статусы"], rows))
    if r["errors"]:
        L.append("")
        L.append("Расхождения с разметкой:")
        L.append("")
        L.append(table(["файл", "человек", "ожидание", "статус", "пояснение"],
                       [[_short(e["file"]), e["subject"], e["expected"], e["status"], e["reason"] or ""]
                        for e in r["errors"]]))
    syn = r["synthetic"]
    L.append("")
    L.append(f"Синтетические плохие эталоны (в памяти): **отклонено {syn['rejected']} из {syn['total']}**.")
    L.append("")
    L.append(table(["вариант", "n", "отклонено", "статусы"],
                   [[k, str(v["n"]), str(v["rejected"]), ", ".join(f"{a} {b}" for a, b in sorted(v["statuses"].items()))]
                    for k, v in syn["by_variant"].items()]))

    # 3. кадры
    L += ["", "## Кадры проходной (фото)", ""]
    L += _gate_block(s["gate"], threshold=s["pairs"]["threshold"])

    # 4. пары
    L += ["", "## Пары «свой / чужой»", ""]
    L += _pairs_block(s["pairs"])

    # 5–6. подмена и повтор
    L += ["", "## Подмена кадра эталоном", ""]
    L.append("Эталон и его искажённые копии отправлены как кадр проходной; сходство — с исходным эталоном.")
    L.append("")
    L += _replay_block(s["replay_reference"], "честных пар «кадр проходной — свой эталон»")
    L += ["", "## Повтор кадра", ""]
    L.append("Слегка изменённая копия кадра проходной против оригинала; честные — разные кадры одного человека.")
    L.append("")
    L += _replay_block(s["replay_frame"], "честных пар «разные кадры одного человека»")

    # 7. пропуска
    sp = s["spoofs"]
    L += ["", "## Пропуска и другие подделки как кадр", ""]
    if sp["n"]:
        L.append(f"Всего {sp['n']}; статусы: " + ", ".join(f"{k} {v}" for k, v in sorted(sp["statuses"].items()))
                 + f"; **пропущено как совпадение {sp['accepted']}**.")
        L.append("")
        L.append(table(["файл", "человек", "вид", "статус", "пояснение", "сходство со своим", "лицо, px",
                        "liveness", "людей"],
                       [[_short(x["file"]), x["subject"], x["kind"], x["gate_status"], x["gate_reason"] or "",
                         x["sim_own"], x["face_px"], x["liveness"], x["people"]] for x in sp["rows"]]))
    else:
        L.append("Нет строк с подделками.")

    # 8. liveness
    lv = s.get("liveness")
    L += ["", "## Живость по одному кадру", ""]
    if lv:
        L.append(f"Порог {_f(lv['threshold'])}. Живые (кадры проходной с лицом): отклонено "
                 f"**{_of(lv['live']['rejected'], lv['live']['n'])}** (BPCER {_f(lv['live']['bpcer'])}). "
                 f"Подделки (пропуск, экран, распечатка): пропущено **{_of(lv['spoof']['accepted'], lv['spoof']['n'])}** "
                 f"(APCER {_f(lv['spoof']['apcer'])}). Эталон как кадр: «не живое» "
                 f"{_of(lv['reference_as_probe']['rejected'], lv['reference_as_probe']['n'])}.")
        L.append("")
        L.append(table(["порог", "живых отклонено", "подделок пропущено"],
                       [[f"{x['t']:.2f}", f"{x['live_rejected']}/{x['live_n']}",
                         f"{x['spoof_accepted']}/{x['spoof_n']}"] for x in lv["grid"]]))
    else:
        L.append("Адаптер не задан — раздел пропущен.")

    # 9. люди
    pe = s.get("people")
    L += ["", "## Подсчёт людей в кадре", ""]
    if pe:
        e1 = pe["expected_one"]
        L.append(f"Порог {_f(pe['threshold'])}. Размеченные кадры с одним человеком: ровно 1 — "
                 f"**{_of(e1['exact_one'], e1['n'])}**, 0 — {e1['zero']}, 2 и больше — **{e1['two_plus']}**. "
                 f"Все размеченные: точно {_of(pe['labeled']['exact'], pe['labeled']['n'])}.")
        L.append("")
        L.append("Ожидаемо людей × насчитано (размеченные фото):")
        L.append("")
        L.append(_people_matrix(pe["labeled"]["matrix"]))
        L.append("")
        L.append(f"Синтетика в памяти: точно {_of(pe['synthetic']['exact'], pe['synthetic']['n'])}.")
        L.append("")
        L.append(_people_matrix(pe["synthetic"]["matrix"]))
        L.append("")
        L.append(table(["вариант", "n", "точно", "насчитано", "статус кадра (качество)"],
                       [[k, str(v["n"]), str(v["exact"]), ", ".join(f"{a}: {b}" for a, b in sorted(v["counts"].items())),
                         ", ".join(f"{a} {b}" for a, b in sorted(v["gate_statuses"].items()))]
                        for k, v in pe["synthetic"]["by_variant"].items()]))
        m = pe["multi_person_rows"]
        L.append("")
        L.append(f"Размеченные `multi_person`: отказ «по одному» {_of(m['rejected_as_multi'], m['n'])}.")
        if pe["clips"]["n"]:
            L.append(f"Ролики (макс. по кадрам): точно {_of(pe['clips']['exact'], pe['clips']['n'])}.")
            L.append("")
            L.append(_people_matrix(pe["clips"]["matrix"]))
        if pe["errors"]:
            L.append("")
            L.append(table(["файл", "ожидалось", "насчитано", "теги"],
                           [[_short(x["file"]), str(x["expected"]), str(x["counted"]), x["tags"]]
                            for x in pe["errors"]]))
    else:
        L.append("Адаптер не задан — раздел пропущен.")

    # 10. ролики
    cl = s.get("clips")
    L += ["", "## Ролики", ""]
    if cl:
        L.append(f"Роликов {cl['n']}, обработано кадров {cl['frames']}. Итог ролика — по лучшему кадру "
                 f"(сначала качество, затем сходство), людей — максимум по кадрам.")
        L.append("")
        L += _gate_block(cl["gate"], "Ролики. ", threshold=cl["pairs"]["threshold"])
        L.append("")
        cp = cl["pairs"]
        L.append(f"Пары по роликам на пороге {_f(cp['threshold'])}: свои принято "
                 f"{_of(cp['genuine']['accepted'], cp['genuine']['n'])}; чужие ложно "
                 f"{_of(cp['impostor']['false_accepts'], cp['impostor']['n'])} (лучший кадр под каждого чужого); "
                 f"похожие ложно {_of(cp['similar']['false_accepts'], cp['similar']['n'])}; зазор {_f(cp['gap'])}.")
    else:
        L.append("В разметке нет роликов — раздел пропущен.")

    # 11. живость по последовательности
    sq = s.get("seq_liveness")
    L += ["", "## Живость по нескольким кадрам", ""]
    if sq:
        st = sq["static_synthetic"]
        L.append(f"Порог {_f(sq['threshold'])}. Живые ролики: отклонено "
                 f"{_of(sq['live_clips']['rejected'], sq['live_clips']['n'])}; ролики-подделки: пропущено "
                 f"{_of(sq['spoof_clips']['accepted'], sq['spoof_clips']['n'])}; статичные последовательности "
                 f"(фото с дрожанием, в памяти): принято за живое **{_of(st['accepted_as_live'], st['n'])}**.")
        L.append("")
        L.append(table(["порог", "живых отклонено", "подделок пропущено", "статичных пропущено"],
                       [[f"{x['t']:.2f}", f"{x['live_rejected']}/{x['live_n']}",
                         f"{x['spoof_accepted']}/{x['spoof_n']}", f"{x['static_accepted']}/{x['static_n']}"]
                        for x in sq["grid"]]))
    else:
        L.append("Адаптер не задан — раздел пропущен.")

    # 12. время
    tm = s.get("timing")
    L += ["", "## Время на CPU", ""]
    if tm:
        names = {"people": "люди", "detect": "детекция лица", "embed": "эмбеддинг", "liveness": "liveness",
                 "total": "итого обработка кадра"}
        rows = []
        for k, title in names.items():
            v = tm["stages"].get(k)
            if v:
                rows.append([title, str(v["n"]), _f(v["median_ms"], 1), _f(v["p95_ms"], 1)])
        for k, title in (("decode_image", "декодирование файла"), ("decode_video_frame", "декодирование кадра видео"),
                         ("seq_liveness_per_clip", "живость по ролику (на ролик)")):
            v = tm.get(k)
            if v:
                rows.append([title, str(v["n"]), _f(v["median_ms"], 1), _f(v["p95_ms"], 1)])
        L.append(table(["этап", "n", "медиана, мс", "p95, мс"], rows))
        L.append("")
        inp = tm["input"]
        L.append(f"- Кадров в секунду в один поток обработки (1000 / медиана): **{_f(tm['fps_single_stream'], 1)}**"
                 f"; с декодированием файла: {_f(tm['fps_single_stream_with_decode'], 1)}.")
        if tm.get("parallel"):
            pp = tm["parallel"]
            L.append(f"- Параллельно {pp['workers']} потоков: {_f(pp.get('fps'), 1)} кадров/с на {pp['frames']} кадрах "
                     f"(ошибок {pp.get('errors')}).")
        L.append(f"- Вход: медиана {inp['median_width']}×{inp['median_height']}, файл {inp['median_bytes']} байт.")
        th = tm["threads"]
        L.append(f"- CPU: {th['cpu_count']} логических, доступно процессу {th['affinity']}; "
                 f"переменные потоков: {th['env'] or 'не заданы'}; потоки адаптера: {th['backend_threads']}.")
    else:
        L.append("Отключено ключом --no-timing.")
    L.append("")
    L.append("Подробности — per_image.csv, pairs.csv, variants.csv, synthetic.csv"
             + (", clip_frames.csv" if cl else "") + ". Причины для людей: "
             + "; ".join(f"`{k}` — {v}" for k, v in REASON_TEXT.items() if k not in ("accept", "match")) + ".")
    return "\n".join(L) + "\n"


def _people_matrix(m: dict) -> str:
    cols = ["0", "1", "2", "3+"]
    return table(["ожидаемо \\ насчитано"] + cols,
                 [[k] + [str(v.get(c, 0)) for c in cols] for k, v in m.items()])
