import os
import subprocess
from pathlib import Path
from string import Template

import pytest
import yaml
from conftest import load_repo_module

ROOT = Path(__file__).parents[1]
PIPELINE = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
UBUNTU_RELEASES = yaml.safe_load((ROOT / "data/ubuntu_releases.yml").read_text())["releases"]
IMAGE_STORE = load_repo_module("mise-tasks/ci/qemu_image_store.py")


def test_ansible_config_is_global() -> None:
    assert PIPELINE["variables"]["ANSIBLE_CONFIG"] == "$CI_PROJECT_DIR/ansible.cfg"
    assert "HOMELAB_CI_ARM" not in PIPELINE["variables"]
    assert PIPELINE["detect"]["script"][-1] == 'mise run ci:detect --target "$HOMELAB_CI_TARGET"'


def test_child_pipeline_forwards_pipeline_variables() -> None:
    assert PIPELINE["test_cells"]["trigger"]["forward"]["pipeline_variables"] is True


def test_api_pipelines_run_regular_jobs() -> None:
    source_rule = '$CI_PIPELINE_SOURCE == "web" || $CI_PIPELINE_SOURCE == "push" || $CI_PIPELINE_SOURCE == "api"'

    assert PIPELINE[".standard_pipeline"]["rules"] == [{"if": source_rule}]
    assert PIPELINE["detect"]["rules"][1]["if"] == source_rule
    assert PIPELINE["test_cells"]["rules"] == [{"if": source_rule}]
    for job in ("lint", "unit_tests"):
        assert ".standard_pipeline" in PIPELINE[job]["extends"]


def test_lab_qemu_image_is_published_for_supported_releases() -> None:
    job = PIPELINE["qemu_image:lab:x86"]

    assert job["parallel"]["matrix"] == [{"UBUNTU": ["noble", "resolute"]}]
    # lab's persistent shell runner must not keep the bake role's token.
    assert job["after_script"] == ['rm -f "$CI_PROJECT_DIR/.aws_web_identity_token"']
    assert job["resource_group"] == "qemu_image_lab_x86_$UBUNTU"
    assert job["script"] == ['mise run packer:publish-qemu lab --ubuntu "$UBUNTU" --promote']
    assert job["extends"] == ".protected_manual_job"
    assert {
        name.removeprefix("qemu_image:").removesuffix(":x86")
        for name in PIPELINE
        if name.startswith("qemu_image:") and name.endswith(":x86")
    } == IMAGE_STORE.VALID_MACHINES


def test_qemu_host_ami_uses_one_architecture_matrix_and_promotion_flow() -> None:
    job = PIPELINE["qemu_host_ami"]

    # The hosted amd64 job only drives Packer. The AMI's reduced ARM toolset
    # comes from qemu_host.pkr.hcl, not from this job's environment.
    assert job["parallel"]["matrix"] == [
        {"QEMU_HOST_ARCHITECTURE": "x86_64"},
        {"QEMU_HOST_ARCHITECTURE": "aarch64"},
    ]
    assert job["resource_group"] == "ami-qemu-host-$QEMU_HOST_ARCHITECTURE"
    assert {
        'source mise-tasks/ci/aws-oidc.sh arn:aws:iam::000390721279:role/homelab-ci-bake "qemu-host-ami-$CI_JOB_ID" --region "$AWS_REGION"',
        'mise run packer:qemu-host-ami --architecture "$QEMU_HOST_ARCHITECTURE" '
        '${NEXT_UBUNTU:+--ubuntu "$NEXT_UBUNTU"} --promote',
    } <= set(job["script"])


def test_qemu_host_ami_cleanup_runs_after_a_script_timeout() -> None:
    job = PIPELINE["qemu_host_ami"]
    script, after_script, total = (
        int(duration.removesuffix("m"))
        for duration in (
            job["variables"]["RUNNER_SCRIPT_TIMEOUT"],
            job["variables"]["RUNNER_AFTER_SCRIPT_TIMEOUT"],
            job["timeout"],
        )
    )

    # GitLab runs after_script on a timeout only when the script's own deadline
    # expires inside the job's, and the cleanup's instance-terminated wait
    # alone can take ten minutes.
    assert script + after_script < total
    assert after_script > 10


@pytest.mark.parametrize("next_ubuntu", [None, "noble"])
def test_ci_image_builds_and_publishes_the_selected_release(tmp_path: Path, next_ubuntu: str | None) -> None:
    docker = tmp_path / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >>"$DOCKER_LOG"\n')
    docker.chmod(0o755)
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DOCKER_LOG": str(tmp_path / "docker.log"),
        "CI_HOST_UBUNTU": PIPELINE["variables"]["CI_HOST_UBUNTU"],
        "CI_REGISTRY_IMAGE": "registry.example/homelab",
        "CI_COMMIT_SHA": "0abc",
        "CI_COMMIT_BRANCH": "master",
        "CI_DEFAULT_BRANCH": "master",
        "HOSTED_ARCH": "x86",
    }
    if next_ubuntu:
        env["NEXT_UBUNTU"] = next_ubuntu
    release = next_ubuntu or env["CI_HOST_UBUNTU"]

    subprocess.run(["bash", "-ec", "\n".join(PIPELINE["ci_image"]["script"])], env=env, check=True)

    calls = (tmp_path / "docker.log").read_text().splitlines()
    (build,) = [call for call in calls if call.startswith("build ")]
    assert f"--build-arg UBUNTU_RELEASE={release} " in build
    pushes = {call.removeprefix("push ") for call in calls if call.startswith("push ")}
    assert pushes == {f"registry.example/homelab/ci:{tag}-{release}-x86" for tag in ("0abc", "latest")}
    if not next_ubuntu:
        # Hosted jobs pull the tag the default build moves.
        assert ".hosted_arch" in PIPELINE["zbm_build"]["extends"]
        pulled = {PIPELINE[".hosted"]["image"], PIPELINE["zbm_build"]["image"]}
        assert {Template(image).substitute(env) for image in pulled} <= pushes


def test_fox_image_release_is_in_the_catalog() -> None:
    assert PIPELINE["fox_image"]["variables"]["FOX_UBUNTU"] in UBUNTU_RELEASES


def test_arm_qemu_images_use_frankfurt_builder_jobs() -> None:
    job = PIPELINE["qemu_image:lab:arm"]

    assert job["extends"] == ".protected_manual_job"
    assert PIPELINE[".protected_manual_job"]["rules"] == [
        {"if": '$CI_COMMIT_REF_PROTECTED == "true"', "when": "manual", "allow_failure": True}
    ]
    assert job["tags"] == ["aws-shell-qemu-arm"]
    assert job["parallel"]["matrix"] == [{"UBUNTU": ["noble", "resolute"]}]
    for variable in (
        "HOME",
        "XDG_CONFIG_HOME",
        "PACKER_PLUGIN_PATH",
        "MISE_DATA_DIR",
        "UV_CACHE_DIR",
        "HOMELAB_CI_DIR",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
    ):
        assert "$CI_JOB_ID" in job["variables"][variable]
    assert "--region" not in job["before_script"][-1]
    assert job["after_script"] == ['rm -f "$CI_PROJECT_DIR/.aws_web_identity_token"']

    assert job["resource_group"] == "qemu_image_lab_arm_$UBUNTU"
    assert job["script"] == [
        'mise run packer:publish-qemu lab --ubuntu "$UBUNTU" --architecture aarch64 --upstream --promote'
    ]
    assert {
        name.removeprefix("qemu_image:").removesuffix(":arm")
        for name in PIPELINE
        if name.startswith("qemu_image:") and name.endswith(":arm")
    } == IMAGE_STORE.VALID_MACHINES


def test_bake_role_uses_shared_qemu_image_bucket() -> None:
    terraform = (ROOT / "terraform" / "aws_ci.tf").read_text()

    assert '"aws:RequestedRegion" = local.ci_aws_region' in terraform
    assert "aws_s3_bucket.ci_qemu_images.arn" in terraform
    assert '"${aws_s3_bucket.ci_qemu_images.arn}/*"' in terraform
    assert "ci_qemu_arm_images" not in terraform
