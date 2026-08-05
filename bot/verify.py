import os
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

_verify_key: VerifyKey | None = None


def _get_key() -> VerifyKey:
    global _verify_key
    if _verify_key is None:
        _verify_key = VerifyKey(bytes.fromhex(os.environ["DISCORD_PUBLIC_KEY"]))
    return _verify_key


def verify_signature(body: bytes, signature: str, timestamp: str) -> bool:
    try:
        _get_key().verify(timestamp.encode() + body, bytes.fromhex(signature))
        return True
    except (BadSignatureError, Exception):
        return False
