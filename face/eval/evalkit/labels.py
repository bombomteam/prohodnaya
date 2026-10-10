"""Чтение разметки (labels.csv). Формат описан в face/eval/README.md."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

KINDS = ("reference", "gate", "badge", "screen", "print", "no_person", "multi_person", "clip")
SPOOF_KINDS = ("badge", "screen", "print")
REF_EXPECTED = ("accept", "reject", "any")
GATE_EXPECTED = ("match", "unusable", "either", "spoof", "multi_person", "disputed")
# Живой человек перед камерой: такие кадры участвуют в парах «свой/чужой».
PROBE_EXPECTED = ("match", "unusable", "either")
# Спорные кадры (тот же ли человек или живой ли кадр — не решить по виду): считаются и
# показываются отдельной группой, но не входят ни в успехи, ни в ошибки, ни в пары и
# статистику для выбора порогов.
DISPUTED_EXPECTED = ("disputed",)
KNOWN_TAGS = (
    "helmet", "hat", "hood", "glasses", "smile", "grimace", "tongue", "hands",
    "profile", "back", "wide_angle", "dark", "backlight", "blur", "small_face",
    "occluded", "borderline", "two_faces",
)
REQUIRED_COLUMNS = ("file", "subject", "kind", "ref_expected", "gate_expected", "tags", "note")
OPTIONAL_COLUMNS = ("people",)


@dataclass
class LabelRow:
    source: int            # номер файла разметки (порядок --labels)
    private: bool          # разметка вне репозитория (например ~/face-data): наружу только имена файлов и числа
    line: int              # номер строки в CSV
    file: str              # путь как в CSV (относительно папки CSV)
    path: Path             # абсолютный путь
    subject: str           # тестовый идентификатор; для приватных источников наружу не выводится
    kind: str
    ref_expected: str
    gate_expected: str
    people: int | None     # ожидаемое число людей в кадре; None — неизвестно
    tags: list[str]
    note: str              # для приватных источников наружу не выводится
    display_file: str = ""
    display_subject: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[int, str]:
        return (self.source, self.subject)

    @property
    def is_clip(self) -> bool:
        return self.kind == "clip"

    @property
    def short(self) -> str:
        """Короткое имя для таблиц отчёта (имя файла или папки)."""
        return Path(self.file).name or self.file


def is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def load_labels(path: Path, source: int) -> tuple[list[LabelRow], list[str]]:
    """Читает один файл разметки. Возвращает строки и предупреждения (без персональных данных)."""
    path = Path(path).expanduser().resolve()
    private = not is_inside(path, REPO_ROOT)
    base = path.parent
    rows: list[LabelRow] = []
    warnings: list[str] = []
    pseudonyms: dict[str, str] = {}
    tag = f"L{source}"
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = [f.strip() for f in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_COLUMNS if c not in fields]
        if missing:
            raise ValueError(f"{path.name}: нет колонок {', '.join(missing)}")
        for line, rec in enumerate(reader, start=2):
            def get(key: str) -> str:
                return (rec.get(key) or "").strip()

            file = get("file")
            if not file:
                warnings.append(f"{tag}:{line}: пустой file — строка пропущена")
                continue
            subject = get("subject")
            kind = get("kind")
            ref_expected = get("ref_expected")
            gate_expected = get("gate_expected")
            problems: list[str] = []
            if kind not in KINDS:
                problems.append(f"неизвестный kind «{kind}»")
            if ref_expected and ref_expected not in REF_EXPECTED:
                problems.append(f"неизвестный ref_expected «{ref_expected}»")
            if gate_expected and gate_expected not in GATE_EXPECTED:
                problems.append(f"неизвестный gate_expected «{gate_expected}»")
            if kind == "clip" and ref_expected:
                warnings.append(f"{tag}:{line}: ролик не бывает эталоном — ref_expected игнорируется")
                ref_expected = ""
            people_raw = get("people")
            people: int | None = None
            if people_raw:
                try:
                    people = int(people_raw)
                    if people < 0:
                        raise ValueError
                except ValueError:
                    problems.append(f"people «{people_raw}» не целое ≥ 0")
                    people = None
            tags = [t.strip() for t in get("tags").split(";") if t.strip()]
            unknown = [t for t in tags if t not in KNOWN_TAGS]
            if unknown:
                warnings.append(f"{tag}:{line}: незнакомые теги {', '.join(unknown)}")
            if not subject and (kind == "reference" or gate_expected in PROBE_EXPECTED + DISPUTED_EXPECTED):
                problems.append("пустой subject")
            abs_path = (base / file).resolve()
            if private:
                # наружу — только имя файла (папки могут называться по людям) и номер строки
                display_subject = pseudonyms.setdefault(subject, f"{tag}-S{len(pseudonyms) + 1:02d}")
                display_file = f"{tag}#{line}/{Path(file).name or file}"
            else:
                display_subject = subject
                try:
                    display_file = abs_path.relative_to(REPO_ROOT).as_posix()
                except ValueError:
                    display_file = file
            rows.append(LabelRow(
                source=source, private=private, line=line, file=file, path=abs_path,
                subject=subject, kind=kind, ref_expected=ref_expected,
                gate_expected=gate_expected, people=people, tags=tags, note=get("note"),
                display_file=display_file, display_subject=display_subject, problems=problems,
            ))
    return rows, warnings
