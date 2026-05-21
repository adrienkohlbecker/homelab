"""Ansible filter: subtract a list of CIDRs from a base set.

Used by roles/wireguard/templates/wg.conf.j2 to compute the all-traffic
AllowedIPs as ``['0.0.0.0/0', '::/0'] | cidr_exclude(<RFC1918+...>)``
rather than maintaining a hand-curated complement of ~70 CIDRs.

The wg-quick implementation does NOT yet support the native ``!cidr``
negation syntax on the wireguard-tools version that ships on jammy
(1.0.20210914) -- so we precompute the inclusion set in jinja.
"""

from netaddr import IPSet


def cidr_exclude(base, excludes):
    return [str(c) for c in (IPSet(base) - IPSet(excludes)).iter_cidrs()]


class FilterModule:
    def filters(self):
        return {"cidr_exclude": cidr_exclude}
