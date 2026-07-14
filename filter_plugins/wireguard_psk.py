"""Ansible filter deriving WireGuard peer-pair PSKs from a vaulted seed.

The filter sorts the two peer names so both peers render the same
``base64(HMAC-SHA256(seed, pair))`` key without per-pair secret files.
"""

import base64
import hmac


def wireguard_psk(peers, seed):
    pair = "-".join(sorted(peers))
    return base64.b64encode(hmac.digest(seed.encode(), pair.encode(), "sha256")).decode()


class FilterModule:
    def filters(self):
        return {"wireguard_psk": wireguard_psk}
