"""Minimal RFC 6238 TOTP (authenticator app) helpers, stdlib only."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

ISSUER = "Pulse AI"
DIGITS = 6
PERIOD = 30


def generate_secret() -> str:
    # 160-bit secret, base32 (the format authenticator apps expect)
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def provisioning_uri(secret: str, account: str) -> str:
    label = quote(f"{ISSUER}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&algorithm=SHA1&digits={DIGITS}&period={PERIOD}"


def _code_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** DIGITS)).zfill(DIGITS)


def verify_code(secret: str, code: str, window: int = 1) -> bool:
    """Accept the current code plus/minus `window` periods to tolerate clock drift."""
    code = (code or "").strip().replace(" ", "")
    if not secret or len(code) != DIGITS or not code.isdigit():
        return False
    now = int(time.time() // PERIOD)
    return any(
        hmac.compare_digest(_code_at(secret, now + drift), code)
        for drift in range(-window, window + 1)
    )
