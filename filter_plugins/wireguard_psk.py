"""Ansible filter deriving WireGuard peer-pair PSKs from a vaulted seed.

The caller passes a sorted pair name; both peers render the same
``base64(HMAC-SHA256(seed, pair))`` key without per-pair secret files.
"""

import base64
import hashlib
import hmac


def wireguard_psk(pair, seed):
    return base64.b64encode(hmac.digest(seed.encode(), pair.encode(), hashlib.sha256)).decode()


class FilterModule:
    def filters(self):
        return {"wireguard_psk": wireguard_psk}
