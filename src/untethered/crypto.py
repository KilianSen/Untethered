"""
Untethered Cryptography: RFC 2104 HMAC-SHA256 & Constant-Time Verification
"""
try:
    import uhashlib as hashlib
    import ubinascii as binascii
except ImportError:
    import hashlib
    import binascii


def _compute_hmac_sha256(key, message):
    """Computes standard RFC 2104 HMAC-SHA256 signature."""
    if isinstance(key, str):
        key = key.encode("utf-8")
    if isinstance(message, str):
        message = message.encode("utf-8")

    block_size = 64
    if len(key) > block_size:
        h = hashlib.sha256()
        h.update(key)
        key = h.digest()
    if len(key) < block_size:
        key = key + b"\x00" * (block_size - len(key))

    o_pad = bytes((x ^ 0x5c) for x in key)
    i_pad = bytes((x ^ 0x36) for x in key)

    inner = hashlib.sha256()
    inner.update(i_pad)
    inner.update(message)
    inner_digest = inner.digest()

    outer = hashlib.sha256()
    outer.update(o_pad)
    outer.update(inner_digest)
    return binascii.hexlify(outer.digest()).decode("ascii")


def _constant_time_compare(val1, val2):
    """Timing-attack resistant comparison of two hex digest strings."""
    if not isinstance(val1, str) or not isinstance(val2, str):
        return False
    if len(val1) != len(val2):
        return False
    diff = 0
    for ch1, ch2 in zip(val1, val2):
        diff |= ord(ch1) ^ ord(ch2)
    return diff == 0
