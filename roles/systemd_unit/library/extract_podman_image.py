#!/usr/bin/python3
"""Ansible module: list podman image refs declared by a systemd unit.

Asks systemd via D-Bus (busctl) for the parsed ExecStart, ExecStartPre, and
ExecStartPost properties of the named unit. For each invocation that runs
`podman run`, walks the argv option-by-option (consulting the schema parsed
from `podman run --help`) and returns the first positional, which is the
image. Each .service file remains the single source of truth for its image;
playbooks call this module by unit name and never restate the image.

Also returns the unit's `User=` property so the caller can pre-pull into
the right podman store: system units that run as root pull into
/var/lib/containers/storage/, system units with a User= override pull
into /var/lib/containers/rootless/<user>/ — and the wrong store at
pre-pull time is invisible until systemctl start busts TimeoutStartSec
on a cold image fetch from inside the unit's User= shell.

Args:
    name: Unit name (with or without the `.service` suffix).

Returns:
    images: Sorted list of distinct image refs.
    user:   The unit's User= directive; empty string when unset (systemd
            default of root). Callers should treat "" and "root" as the
            same case.
"""

import json
import re
import subprocess

from ansible.module_utils.basic import AnsibleModule

DOCUMENTATION = r"""
---
module: extract_podman_image
short_description: List podman image refs declared by a systemd unit's ExecStart*
options:
  name:
    description: Unit name; the `.service` suffix is added if missing.
    required: true
    type: str
"""

EXAMPLES = r"""
- name: Get images referenced by transmission.service
  extract_podman_image:
    name: transmission
  register: out

- debug:
    var: out.images
"""

RETURN = r"""
images:
  description: Distinct podman image refs, one per `podman run` invocation.
  type: list
  elements: str
user:
  description: The unit's User= directive; empty string when unset (root).
  type: str
"""

_EXEC_PROPS = ("ExecStart", "ExecStartPre", "ExecStartPost")

# Help-line shape: optional `-x,` short, then `--long`, then optional type
# token (anything non-whitespace -- `string`, `strings`, `int`, `ARCH`,
# `<number>[<unit>]`, etc.), then a 2+ space gap before the description.
# A type token's presence means the option takes a value; absence => flag.
_HELP_LINE = re.compile(r"^\s+(?:-(\w),\s+)?(--[\w-]+)(\s+(\S+))?\s{2,}")


def bus_label_escape(name):
    """Escape per systemd's bus_label_escape: only ASCII alphanumerics survive."""
    return "".join(c if c.isascii() and c.isalnum() else f"_{ord(c):02x}" for c in name)


def _parse_run_help():
    """Return the set of `podman run` flags that take no value.

    Parsing podman's own help means we don't hardcode a flag list -- if podman
    adds, removes, or changes options, our parser stays in sync as long as
    the cobra-generated help format remains consistent. Anything not in this
    set (including options we failed to parse) is treated as `--opt value`.
    """
    flags_no_value = set()
    try:
        result = subprocess.run(
            ["podman", "run", "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        return flags_no_value
    for line in result.stdout.splitlines():
        m = _HELP_LINE.match(line)
        if not m:
            continue
        short, long_, has_value = m.group(1), m.group(2), bool(m.group(3))
        if has_value:
            continue
        flags_no_value.add(long_)
        if short:
            flags_no_value.add(f"-{short}")
    return flags_no_value


def is_podman_run(argv):
    """True iff argv looks like a `podman run ...` invocation."""
    return len(argv) >= 3 and argv[1] == "run" and isinstance(argv[0], str) and argv[0].rsplit("/", 1)[-1] == "podman"


def find_image(argv, flags_no_value):
    """Return the image arg from a `podman run ...` argv, or None.

    Walks options using the schema in `flags_no_value` so unit templates that
    pass a CMD after the image (e.g. redis's trailing
    `redis-server --save 60 1 --loglevel warning`) still resolve to the image
    rather than to some trailing CMD arg.
    """
    if not is_podman_run(argv):
        return None

    i = 2
    while i < len(argv):
        arg = argv[i]
        if not isinstance(arg, str) or not arg.startswith("-"):
            return arg
        if "=" in arg:
            i += 1
        elif arg in flags_no_value:
            i += 1
        else:
            # Known value-taker, or an option we couldn't classify -- treat
            # as `--opt value` and skip both. Erring this way means an
            # unrecognized flag would over-consume one positional, but the
            # consequence is just "image not found" rather than confusing
            # some flag value with the image.
            i += 2
    return None


def _get_properties(unit_path, props):
    """Fetch multiple properties in a single busctl call.

    busctl outputs one JSON object per property, one per line.
    Returns a dict mapping property name to its parsed JSON object,
    or an empty dict on failure.
    """
    result = subprocess.run(
        [
            "busctl",
            "--json=short",
            "--timeout=5",
            "get-property",
            "org.freedesktop.systemd1",
            f"/org/freedesktop/systemd1/unit/{unit_path}",
            "org.freedesktop.systemd1.Service",
        ]
        + list(props),
        capture_output=True,
        text=True,
        check=False,
        # --timeout=5 is inert for get-property (only applies to busctl call);
        # Python-level timeout is the real guard against a hung D-Bus.
        timeout=10,
    )
    if result.returncode != 0:
        return {}
    lines = result.stdout.splitlines()
    # busctl --json=short emits exactly one compact JSON line per requested
    # property, in request order; the positional zip below relies on that. If
    # the counts ever diverge (e.g. a future flag change drops --json=short
    # and a value wraps), fail loud rather than silently misattributing User=
    # to another property's value and pre-pulling into the wrong podman store.
    if len(lines) != len(props):
        raise ValueError(
            f"busctl returned {len(lines)} line(s) for {len(props)} "
            f"requested properties; expected one JSON line each (--json=short)."
        )
    out = {}
    for line, prop in zip(lines, props):
        try:
            out[prop] = json.loads(line)
        except json.JSONDecodeError:
            pass  # skip unparseable lines
    return out


def extract_images(name):
    """Return (images, user, unresolved). `unresolved` lists podman-run argvs we
    saw but couldn't extract an image from — typically a parser drift."""
    if not name.endswith(".service"):
        name = name + ".service"
    unit_path = bus_label_escape(name)
    flags_no_value = _parse_run_help()

    all_props = _get_properties(unit_path, list(_EXEC_PROPS) + ["User"])

    images = set()
    unresolved = []
    for prop in _EXEC_PROPS:
        obj = all_props.get(prop)
        if not obj:
            continue
        for entry in obj.get("data") or []:
            if not isinstance(entry, list) or len(entry) < 2:
                continue
            argv = entry[1]
            if not isinstance(argv, list) or not is_podman_run(argv):
                continue
            image = find_image(argv, flags_no_value)
            if image is not None:
                images.add(image)
            else:
                unresolved.append(" ".join(str(a) for a in argv))

    user_obj = all_props.get("User")
    user = ""
    if user_obj:
        data = user_obj.get("data")
        if isinstance(data, str):
            user = data

    return sorted(images), user, unresolved


def main():
    module = AnsibleModule(
        argument_spec=dict(name=dict(type="str", required=True)),
        supports_check_mode=True,
    )
    images, user, unresolved = extract_images(module.params["name"])
    # If we saw `podman run` argvs but couldn't pull an image out of any of
    # them, the schema-aware walker has drifted from podman's flag set.
    # Fail the play rather than silently skipping pre-pull and falling back
    # to a slow `podman pull` inside `systemctl start`'s TimeoutStartSec.
    if unresolved:
        module.fail_json(
            msg=(
                f"extract_podman_image: {module.params['name']} has "
                f"{len(unresolved)} `podman run` ExecStart line(s) with no "
                f"resolvable image — the argv walker is out of sync with "
                f"podman's flag set. Update the parser before pre-pull "
                f"silently regresses."
            ),
            argvs=unresolved,
        )
    module.exit_json(changed=False, images=images, user=user, unresolved=unresolved)


if __name__ == "__main__":
    main()
