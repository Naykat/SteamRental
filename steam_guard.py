import base64
import hashlib
import hmac
import struct
import time


def generate_code(shared_secret: str) -> str:
    timestamp = int(time.time())
    time_bytes = struct.pack(">Q", timestamp // 30)
    secret = base64.b64decode(shared_secret)
    mac = hmac.new(secret, time_bytes, hashlib.sha1).digest()
    offset = mac[19] & 0x0F
    code_int = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    chars = "23456789BCDFGHJKMNPQRTVWXY"
    code = ""
    for _ in range(5):
        code += chars[code_int % len(chars)]
        code_int //= len(chars)
    return code
