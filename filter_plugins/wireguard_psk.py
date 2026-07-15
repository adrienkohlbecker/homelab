"""Ansible filter deriving WireGuard peer-pair PSKs from a vaulted seed.

The filter sorts the two peer names so both peers render the same
``base64(HMAC-SHA256(seed, pair))`` key without per-pair secret files.
"""

import base64
import hmac

from ansible.errors import AnsibleError


def wireguard_psk(peers, seed):
    if isinstance(peers, str):
        raise AnsibleError("wireguard_psk requires exactly two non-empty peer names")

    try:
        peer_names = list(peers)
    except TypeError as error:
        raise AnsibleError("wireguard_psk requires exactly two non-empty peer names") from error

    if len(peer_names) != 2 or not all(isinstance(name, str) and name for name in peer_names):
        raise AnsibleError("wireguard_psk requires exactly two non-empty peer names")

    pair = "-".join(sorted(peer_names))
    return base64.b64encode(hmac.digest(seed.encode(), pair.encode(), "sha256")).decode()


class FilterModule:
    def filters(self):
        return {"wireguard_psk": wireguard_psk}
