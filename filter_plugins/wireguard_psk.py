"""Ansible filter: derive a WireGuard preshared key for a peer-pair.

``'<a>-<b>' | wireguard_psk(seed)`` returns ``base64(HMAC-SHA256(seed,
'<a>-<b>'))`` -- 32 bytes, the exact shape a WireGuard ``PresharedKey``
wants. Used by roles/wireguard/templates/wg.conf.j2 so PSKs are never
stored on disk: both ends of a pair render from the same vaulted
``wireguard_psk_seed`` and the same sorted pair name, so they compute an
identical key without a shared file. Adding/removing a peer just changes
which pairs get derived -- nothing to generate or prune.

The pair name MUST be sorted (the template passes ``[a, b] | sort |
join('-')``) so ``lab-phone`` and ``phone-lab`` map to one key.

Security: the only stored secret is the seed, vaulted exactly like the
private keys. An attacker with the vault already has every private key,
so deriving all PSKs from one seed adds no exposure beyond that boundary;
and the post-quantum property holds -- the PSK stays secret from anyone
who only observes the (classical) handshake.
"""

import base64
import hashlib
import hmac


def wireguard_psk(pair, seed):
    digest = hmac.new(seed.encode(), pair.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


class FilterModule:
    def filters(self):
        return {"wireguard_psk": wireguard_psk}
