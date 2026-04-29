#!/usr/bin/python3
"""Ansible module: list podman image refs declared by a systemd unit.

Asks systemd via D-Bus (busctl) for the parsed ExecStart, ExecStartPre, and
ExecStartPost properties of the named unit. For each invocation that runs
`podman run`, walks the argv option-by-option (consulting the schema parsed
from `podman run --help`) and returns the first positional, which is the
image. Each .service file remains the single source of truth for its image;
playbooks call this module by unit name and never restate the image.

Args:
    name: Unit name (with or without the `.service` suffix).

Returns:
    images: Sorted list of distinct image refs.
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
"""

_EXEC_PROPS = ("ExecStart", "ExecStartPre", "ExecStartPost")

# Help-line shape: optional `-x,` short, then `--long`, then optional type
# token (anything non-whitespace -- `string`, `strings`, `int`, `ARCH`,
# `<number>[<unit>]`, etc.), then a 2+ space gap before the description.
# A type token's presence means the option takes a value; absence => flag.
_HELP_LINE = re.compile(
    r"^\s+(?:-(\w),\s+)?(--[\w-]+)(\s+(\S+))?\s{2,}"
)


def bus_label_escape(name):
    """Escape per systemd's bus_label_escape: only ASCII alphanumerics survive."""
    return "".join(
        c if c.isascii() and c.isalnum() else f"_{ord(c):02x}" for c in name
    )


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
            capture_output=True, text=True, check=False,
        )
    except (OSError, subprocess.SubprocessError):
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


def find_image(argv, flags_no_value):
    """Return the image arg from a `podman run ...` argv, or None.

    Walks options using the schema in `flags_no_value` so unit templates that
    pass a CMD after the image (e.g. redis's trailing
    `redis-server --save 60 1 --loglevel warning`) still resolve to the image
    rather than to some trailing CMD arg.
    """
    if len(argv) < 3 or argv[1] != "run":
        return None
    if not isinstance(argv[0], str) or argv[0].rsplit("/", 1)[-1] != "podman":
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


def _get_property(unit_path, prop):
    result = subprocess.run(
        [
            "busctl", "--json=short", "get-property",
            "org.freedesktop.systemd1",
            f"/org/freedesktop/systemd1/unit/{unit_path}",
            "org.freedesktop.systemd1.Service",
            prop,
        ],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def extract_images(name):
    if not name.endswith(".service"):
        name = name + ".service"
    unit_path = bus_label_escape(name)
    flags_no_value = _parse_run_help()

    images = set()
    for prop in _EXEC_PROPS:
        obj = _get_property(unit_path, prop)
        if not obj:
            continue
        for entry in obj.get("data") or []:
            if not isinstance(entry, list) or len(entry) < 2:
                continue
            argv = entry[1]
            if not isinstance(argv, list):
                continue
            image = find_image(argv, flags_no_value)
            if image is not None:
                images.add(image)
    return sorted(images)


def main():
    module = AnsibleModule(
        argument_spec=dict(name=dict(type="str", required=True)),
        supports_check_mode=True,
    )
    images = extract_images(module.params["name"])
    module.exit_json(changed=False, images=images)


if __name__ == "__main__":
    main()
