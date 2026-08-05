import os
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

_key = VerifyKey(bytes.fromhex(os.environ["DISCORD_PUBLIC_KEY"]))

def verify_signature(body: bytes, signature: str, timestamp: str) -> bool:
    try:
        _key.verify(timestamp.encode() + body, bytes.fromhex(signature))
        return True
    except (BadSignatureError, Exception):
        return False
