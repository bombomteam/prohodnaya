"""Общие тестовые помощники: телефон рабочего с ключом P-256, заведение человека, активация устройства."""
import base64
from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.repositories import persons as persons_repo


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Phone:
    """Телефон рабочего: ключ P-256 и подпись в формате WebCrypto (сырые r||s)."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        n = self.key.public_key().public_numbers()
        self.jwk = {"kty": "EC", "crv": "P-256", "x": _b64u(n.x.to_bytes(32, "big")), "y": _b64u(n.y.to_bytes(32, "big"))}
        self.device_id: str | None = None
        self.person_id: int | None = None  # заполняет activate

    def sign(self, message: str) -> str:
        r, s = decode_dss_signature(self.key.sign(message.encode(), ec.ECDSA(hashes.SHA256())))
        return _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    def pass_code_body(self, ts: str | None = None, device_id: str | None = None) -> dict:
        """Тело POST /api/device/pass-code: подпись над "pass-code|device_id|ts" (ts — строка как отправлена)."""
        device_id = device_id or self.device_id
        ts = ts or datetime.now(timezone.utc).isoformat()
        return {"device_id": device_id, "ts": ts, "signature": self.sign(f"pass-code|{device_id}|{ts}")}


def make_person(db, fio="Иванов Иван", emb="EMB", status="active", model="m1", template=True) -> int:
    person = persons_repo.add(db, fio, None, None)
    person.status = status
    if template:
        persons_repo.set_template(db, person.id, emb, model, f"hash-{emb}", f"{person.id}.jpg")
    db.commit()
    return person.id


def activate(client, person_id: int, phone: Phone) -> None:
    """Реальная активация: код -> /enroll с публичным ключом телефона."""
    code = client.post(f"/api/persons/{person_id}/enroll-code").json()["code"]
    r = client.post("/api/enroll", json={"code": code, "public_key_jwk": phone.jwk})
    assert r.status_code == 200, r.text
    phone.device_id = r.json()["device_id"]
    phone.person_id = person_id


# ---- план, калибровка и зоны ----

def png_bytes(width: int, height: int) -> bytes:
    """Минимальный PNG-заголовок (сигнатура + IHDR) — серверу хватает его, чтобы прочитать размеры."""
    return b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00" + b"\x00" * 4


def jpeg_bytes(width: int, height: int) -> bytes:
    """Минимальный JPEG: SOI + APP0 + SOF0 с размерами."""
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


# план 1000x800 px: lon 37.00..37.01 -> x 0..1000, lat 55.00..54.99 -> y 0..800
CAL_POINTS = [
    {"px": 0, "py": 0, "lat": 55.0, "lon": 37.0},
    {"px": 1000, "py": 0, "lat": 55.0, "lon": 37.01},
    {"px": 0, "py": 800, "lat": 54.99, "lon": 37.0},
]
BIG = [[0, 0], [1000, 0], [1000, 800], [0, 800]]
SMALL = [[400, 300], [600, 300], [600, 500], [400, 500]]  # лежит внутри BIG
# точки: в обеих зонах, только в большой, вне плана
P_BOTH, P_BIG, P_OUT = (54.995, 37.005), (54.999, 37.001), (55.1, 37.005)


def setup_geo(client) -> dict[str, int]:
    """Загружает план, калибровку и две вложенные зоны «Объект» (BIG) и «Склад» (SMALL); возвращает {имя: id}."""
    assert client.put("/api/site/plan", files={"image": ("p.png", png_bytes(1000, 800), "image/png")}).status_code == 200
    assert client.put("/api/site/calibration", json={"points": CAL_POINTS}).status_code == 200
    r = client.put("/api/zones", json={"zones": [{"name": "Объект", "polygon": BIG}, {"name": "Склад", "polygon": SMALL}]})
    assert r.status_code == 200, r.text
    return {z["name"]: z["id"] for z in r.json()["zones"]}
