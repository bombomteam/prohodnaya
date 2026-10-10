"""Проверка подписи телефона: ECDSA P-256 + SHA-256, подпись в формате WebCrypto (сырые r||s)."""
import base64

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature


def _b64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_es256(jwk: dict, message: bytes, sig_b64u: str) -> bool:
    """Верна ли подпись message ключом jwk. Любой мусор на входе (ключ, длина, base64) = False, без исключений."""
    try:
        pub = ec.EllipticCurvePublicNumbers(
            int.from_bytes(_b64u(jwk["x"]), "big"),
            int.from_bytes(_b64u(jwk["y"]), "big"),
            ec.SECP256R1()).public_key()
        raw = _b64u(sig_b64u)
    except (KeyError, ValueError, TypeError):
        return False
    if len(raw) != 64:
        return False
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    try:
        pub.verify(der, message, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False
