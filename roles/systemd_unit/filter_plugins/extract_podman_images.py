"""Jinja filter: extract podman image refs from a systemd unit's text.

Walks each ExecStart* line, parses it with shlex (so embedded shell quoting
in args like --health-cmd is handled), and emits the trailing positional
arg of any `podman run` invocation. Runs on the ansible controller -- no
script ever touches the target host.
"""
import re
import shlex

_EXEC_PREFIX = ("ExecStart=", "ExecStartPre=", "ExecStartPost=")


def extract_podman_images(content):
    content = re.sub(r"\\\n[ \t]*", " ", content)

    images = set()
    for line in content.splitlines():
        if not line.startswith(_EXEC_PREFIX):
            continue
        cmd = line.split("=", 1)[1].lstrip("@-+:!")
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            continue
        for i in range(len(tokens) - 1):
            if tokens[i].rsplit("/", 1)[-1] == "podman" and tokens[i + 1] == "run":
                images.add(tokens[-1])
                break

    return sorted(images)


class FilterModule:
    def filters(self):
        return {"extract_podman_images": extract_podman_images}
