#!/usr/bin/env python3
"""Adopt the newest baked CI worker snapshot into gitlab-runner's config.toml.

fleeting-plugin-hetzner resolves its `image` by name or id only, and Hetzner
snapshots have no name -- so the rendered value is a numeric snapshot id that
goes stale on every worker rebake. Ansible resolves the same label selector at
converge time (roles/gitlab_runner/tasks/main.yml); this script, driven by the
gitlab_runner_image_refresh timer, adopts mid-cycle bakes without a commit or
converge. gitlab-runner re-reads config.toml every ~3s, so a rewrite is picked
up live -- no restart, no drain.

Exits non-zero (a loud failed unit in the journal) when the selector matches
nothing or the API is unreachable; the config is never touched on any failure
path.
"""

import argparse
import json
import os
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

try:
    import tomllib
except ModuleNotFoundError:
    # jammy ships python 3.10 (no stdlib tomllib); the role apt-installs the
    # tomli backport there, same as the config validate in tasks/main.yml.
    import tomli as tomllib

HETZNER_API = "https://api.hetzner.cloud/v1"


def newest_snapshot_id(endpoint: str, token: str, selector: str) -> str:
    query = urllib.parse.urlencode(
        {
            "type": "snapshot",
            "status": "available",
            "label_selector": selector,
            "sort": "created:desc",
            "per_page": 1,
        }
    )
    request = urllib.request.Request(f"{endpoint}/images?{query}", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            images = json.load(response)["images"]
    except urllib.error.URLError as exc:
        sys.exit(f"Hetzner API request failed: {exc}")
    if not images:
        sys.exit(
            f"no available snapshot matches label selector {selector!r}; " "bake one with `mise run packer:worker`"
        )
    return str(images[0]["id"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="path to gitlab-runner config.toml")
    parser.add_argument("--selector", required=True, help="snapshot label selector")
    parser.add_argument("--endpoint", default=HETZNER_API, help="Hetzner API base URL")
    args = parser.parse_args()

    with open(args.config, "rb") as handle:
        config = tomllib.load(handle)
    plugin_config = config["runners"][0]["autoscaler"]["plugin_config"]

    newest = newest_snapshot_id(args.endpoint, plugin_config["token"], args.selector)
    current = str(plugin_config["image"])
    if newest == current:
        print(f"image {current} already current")
        return

    with open(args.config, encoding="utf-8") as handle:
        text = handle.read()
    # Surgical single-line edit, not a TOML re-dump: every other byte of the
    # ansible-rendered file (comments, layout, secrets) must survive intact.
    # Exactly one image key exists (plugin_config); refuse to guess if that
    # shape ever changes.
    pattern = re.compile(r'^(\s*image = )"[^"]*"$', flags=re.MULTILINE)
    if len(pattern.findall(text)) != 1:
        sys.exit('expected exactly one image = "..." line in config; not rewriting')
    rewritten = pattern.sub(rf'\g<1>"{newest}"', text)

    # Atomic same-directory replace so gitlab-runner's 3s config watcher never
    # sees a half-written file; mode (0600 -- the file holds both tokens) and
    # ownership carry over from the original.
    original = os.stat(args.config)
    config_dir = os.path.dirname(os.path.abspath(args.config))
    fd, tmp_path = tempfile.mkstemp(dir=config_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rewritten)
        os.chmod(tmp_path, stat.S_IMODE(original.st_mode))
        if os.geteuid() == 0:
            os.chown(tmp_path, original.st_uid, original.st_gid)
        os.replace(tmp_path, args.config)
    except BaseException:
        os.unlink(tmp_path)
        raise
    print(f"image {current} -> {newest}")


if __name__ == "__main__":
    main()
