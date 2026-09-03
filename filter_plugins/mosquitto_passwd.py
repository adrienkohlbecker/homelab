import base64
import hashlib

_ITERATIONS = 210000


def mosquitto_passwd(passwd, *, salt):
    # Mosquitto requires a 12-byte salt; derive it deterministically from the
    # configured string so the rendered password file remains idempotent.
    salt_bytes = hashlib.sha256(salt.encode()).digest()[:12]
    digest = hashlib.pbkdf2_hmac("sha512", passwd.encode(), salt_bytes, _ITERATIONS)
    b64_salt = base64.b64encode(salt_bytes).decode()
    b64_checksum = base64.b64encode(digest).decode()

    return f"$7${_ITERATIONS}${b64_salt}${b64_checksum}"


class FilterModule:
    def filters(self):
        return {
            "mosquitto_passwd": mosquitto_passwd,
        }
