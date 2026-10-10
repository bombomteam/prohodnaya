"""Защита от забытых заглушек: в backend/app не должно остаться маркера `# STUB`."""
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
MARKER = "# STUB"


def test_no_stubs():
    found = [
        f"{p.relative_to(APP.parent)}:{n}"
        for p in sorted(APP.rglob("*.py"))
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if MARKER in line
    ]
    assert not found, "Остались заглушки (# STUB): " + ", ".join(found)
