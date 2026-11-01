#!/usr/bin/env python3
# [MISE] description="Trigger a GitLab pipeline with the CI dispatch variables"
# [USAGE] flag "--ref <ref>" help="Branch or tag to run" default="master"
# [USAGE] flag "--roles <roles>" help="ROLES dispatch: ALL, SITE, or comma-separated roles"
# [USAGE] flag "--target <target>" help="HOMELAB_CI_TARGET: aws_qemu or lab"
# [USAGE] flag "--base-ref <base_ref>" help="CI_BASE_REF: diff base for change detection"
# [USAGE] flag "--no-arm" help="HOMELAB_CI_ARM=false: drop the ARM lane"
# [USAGE] flag "--host-ubuntu <codename>" help="CI_HOST_UBUNTU: release of the CI runner hosts and hosted job images"
# [USAGE] flag "--next-ubuntu <codename>" help="NEXT_UBUNTU: release the manual ci_image and qemu_host_ami jobs bake"
# [USAGE] flag "--fox-ubuntu <codename>" help="FOX_UBUNTU: release the manual fox_image job bakes"
"""Create a GitLab pipeline carrying the variables .gitlab-ci.yml reads.

Covers the pipeline-level knobs only. Matrix values (UBUNTU, HOSTED_ARCH,
QEMU_HOST_ARCHITECTURE) are left out because a pipeline variable overrides
every matrix entry; secrets live in the project's CI/CD settings.

The variables travel as a JSON body: `glab api -f 'variables[][key]=...'`
creates the pipeline but silently drops them, so after creation this reads the
pipeline's variables back and fails if they differ from what was sent.
"""

import json
import os
import subprocess
import sys
from argparse import ArgumentParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from detect import TARGETS

# Option name -> pipeline variable, in the order they are sent.
VARIABLES = {
    "roles": "ROLES",
    "target": "HOMELAB_CI_TARGET",
    "base_ref": "CI_BASE_REF",
    "no_arm": "HOMELAB_CI_ARM",
    "host_ubuntu": "CI_HOST_UBUNTU",
    "next_ubuntu": "NEXT_UBUNTU",
    "fox_ubuntu": "FOX_UBUNTU",
}


class PipelineVariablesMismatch(RuntimeError):
    """GitLab created the pipeline without the requested variables."""


def pipeline_variables(**options):
    """Return the GitLab `variables` list for the requested dispatch.

    Unset options are omitted so .gitlab-ci.yml's own defaults apply;
    `no_arm=True` becomes HOMELAB_CI_ARM=false, the only value detect reads.
    """
    unknown = options.keys() - VARIABLES.keys()
    if unknown:
        raise TypeError(f"unknown pipeline options: {sorted(unknown)}")
    if options.get("no_arm"):
        options["no_arm"] = "false"
    return [{"key": VARIABLES[name], "value": options[name]} for name in VARIABLES if options.get(name)]


def glab_api(path, method="GET", body=None):
    """Call the GitLab API for the current repository through glab."""
    command = ["glab", "api", "--method", method, path]
    if body is not None:
        command += ["--header", "Content-Type: application/json", "--input", "-"]
    result = subprocess.run(
        command,
        input=None if body is None else json.dumps(body),
        capture_output=True,
        text=True,
        check=True,
        # The origin is gitlab.com; pin glab's host instead of letting it guess.
        env={**os.environ, "GITLAB_HOST": "gitlab.com"},
    )
    return json.loads(result.stdout)


def trigger(ref, variables):
    """Create the pipeline and verify GitLab kept its variables."""
    pipeline = glab_api("projects/:id/pipeline", method="POST", body={"ref": ref, "variables": variables})
    stored = glab_api(f"projects/:id/pipelines/{pipeline['id']}/variables")
    sent = {variable["key"]: variable["value"] for variable in variables}
    kept = {variable["key"]: variable["value"] for variable in stored}
    if kept != sent:
        raise PipelineVariablesMismatch(f"pipeline {pipeline['web_url']} has variables {kept}, expected {sent}")
    return pipeline


def main(args=None):
    parser = ArgumentParser(prog="ci:run")
    parser.add_argument("--ref", default="master")
    parser.add_argument("--roles")
    parser.add_argument("--target", choices=sorted(TARGETS))
    parser.add_argument("--base-ref")
    parser.add_argument("--no-arm", action="store_true")
    parser.add_argument("--host-ubuntu")
    parser.add_argument("--next-ubuntu")
    parser.add_argument("--fox-ubuntu")
    opts = vars(parser.parse_args(args))

    ref = opts.pop("ref")
    variables = pipeline_variables(**opts)
    pipeline = trigger(ref, variables)
    summary = " ".join(f"{variable['key']}={variable['value']}" for variable in variables) or "no variables"
    print(f"{pipeline['web_url']} ({ref}, {summary})")


if __name__ == "__main__":
    main()
