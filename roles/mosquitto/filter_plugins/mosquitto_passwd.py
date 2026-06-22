import base64
import hashlib

from ansible.errors import AnsibleError
from passlib.hash import pbkdf2_sha512
from passlib.utils.binary import ab64_decode

_ITERATIONS = 210000


def mosquitto_passwd(passwd, salt=None):
    if salt is None:
        # A fixed per-account salt keeps the rendered pwfile byte-stable, so the
        # template stays idempotent instead of churning a restart every converge.
        raise AnsibleError("mosquitto_passwd requires an explicit salt")

    # OWASP-recommended PBKDF2-HMAC-SHA512 work factor. The broker reads the
    # count back from the $7$<iterations>$... field, so raising it here needs
    # no mosquitto.conf change.
    # mosquitto's $7$ pwfile format requires the salt to decode to exactly 12
    # bytes (its hardcoded SALT_LEN); any other length is rejected at load with
    # "Unable to decode password salt". The operator-supplied salt is an
    # arbitrary string, so derive a deterministic 12-byte salt from it -- a
    # stable input keeps the rendered pwfile idempotent.
    salt_bytes = hashlib.sha256(salt.encode()).digest()[:12]

    hashed = pbkdf2_sha512.using(
        salt=salt_bytes,
        rounds=_ITERATIONS,
    ).hash(passwd.encode())

    # passlib emits "$pbkdf2-sha512$<rounds>$<salt>$<checksum>" in its adapted
    # base64 alphabet (./ for +/, padding stripped); mosquitto's "$7$" pwfile
    # format wants standard padded base64 for both the salt and the digest.
    # Decode each segment to raw bytes and re-encode -- correct for any salt
    # length, unlike alphabet char-substitution which mishandles the salt's
    # padding (see eclipse-mosquitto#2847).
    _, _, rounds, ab64_salt, ab64_checksum = hashed.split("$")
    b64_salt = base64.b64encode(ab64_decode(ab64_salt.encode("ascii"))).decode("ascii")
    b64_checksum = base64.b64encode(ab64_decode(ab64_checksum.encode("ascii"))).decode("ascii")

    return f"$7${rounds}${b64_salt}${b64_checksum}"


class FilterModule:
    def filters(self):
        return {
            "mosquitto_passwd": mosquitto_passwd,
        }
