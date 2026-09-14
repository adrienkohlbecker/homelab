"""Behavioral tests for the Packer mise task wrappers."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_SH = REPO_ROOT / "mise-tasks" / "packer" / "build.sh"
FIRMWARE_SH = REPO_ROOT / "mise-tasks" / "test" / "firmware.sh"
HETZNER_SH = REPO_ROOT / "mise-tasks" / "packer" / "hetzner.sh"
QEMU_HOST_AMI_SH = REPO_ROOT / "mise-tasks" / "packer" / "qemu-host-ami.sh"
PUBLISH_QEMU_SH = REPO_ROOT / "mise-tasks" / "packer" / "publish-qemu.sh"
QEMU_HOST_TEMPLATE = REPO_ROOT / "packer" / "aws" / "qemu_host.pkr.hcl"
QEMU_HOST_PROVISION_SH = REPO_ROOT / "packer" / "aws" / "files" / "provision_qemu_host.sh"
QEMU_HOST_SCRATCH_SH = REPO_ROOT / "packer" / "aws" / "files" / "homelab_ci_prepare_scratch.sh"
QEMU_POSTPROCESS_SH = REPO_ROOT / "packer" / "scripts" / "postprocess.sh"
QEMU_TEMPLATE = REPO_ROOT / "packer" / "qemu.pkr.hcl"
QEMU_PROVISION_SH = REPO_ROOT / "packer" / "scripts" / "provision.sh"
UBUNTU_CATALOG = REPO_ROOT / "data" / "ubuntu_releases.yml"
UBUNTU_COMPLETION_TASKS = (
    BUILD_SH,
    REPO_ROOT / "mise-tasks" / "packer" / "hetzner.sh",
    QEMU_HOST_AMI_SH,
    PUBLISH_QEMU_SH,
    REPO_ROOT / "mise-tasks" / "test" / "build_box_deps.sh",
    REPO_ROOT / "mise-tasks" / "packer" / "upload-s3.py",
    REPO_ROOT / "mise-tasks" / "ci" / "hydrate-qemu-images.py",
)
PYTHON_USAGE_TASKS = (
    REPO_ROOT / "mise-tasks" / "packer" / "upload-s3.py",
    REPO_ROOT / "mise-tasks" / "ci" / "hydrate-qemu-images.py",
)


def _executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def _environment(tmp_path: Path, ubuntus: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        HOMELAB_CI_DIR=str(tmp_path / "homelab_ci"),
        usage_no_publish="true",
        usage_sources="box",
        usage_ubuntu=ubuntus,
        usage_upstream="false",
    )
    return env


def test_build_runs_once_per_ubuntu(tmp_path: Path) -> None:
    ubuntus = ("noble", "resolute")
    fake_bin = tmp_path / "bin"
    log = tmp_path / "packer.log"
    cache_log = tmp_path / "cache.log"
    _executable(fake_bin / "uname", "#!/bin/sh\nset -eu\nprintf 'Linux\\n'\n")
    _executable(
        fake_bin / "packer",
        "#!/bin/sh\n"
        "set -eu\n"
        'printf "%s\\n" "$*" >>"$PACKER_TEST_LOG"\n'
        'printf "%s\\n" "$PACKER_CACHE_DIR" >>"$PACKER_CACHE_LOG"\n',
    )
    env = _environment(tmp_path, " ".join(ubuntus))
    env.update(
        PATH=f"{fake_bin}:{env['PATH']}",
        PACKER_CACHE_LOG=str(cache_log),
        PACKER_TEST_LOG=str(log),
    )

    result = subprocess.run(["bash", str(BUILD_SH)], cwd=REPO_ROOT, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert len(calls) == len(ubuntus)
    for ubuntu, call in zip(ubuntus, calls, strict=True):
        assert f"ubuntu_name={ubuntu}" in call
        assert f"output_directory={env['HOMELAB_CI_DIR']}/{ubuntu}" in call
        assert "-only=qemu.box" in call
    assert cache_log.read_text().splitlines() == [f"{env['HOMELAB_CI_DIR']}/packer_cache"] * len(ubuntus)


def test_publish_qemu_builds_and_uploads_lab(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    log = tmp_path / "mise.log"
    _executable(
        fake_bin / "mise",
        '#!/bin/sh\nset -eu\nprintf "%s\\n" "$*" >>"$MISE_TEST_LOG"\n',
    )
    env = dict(os.environ)
    env.update(
        MISE_TEST_LOG=str(log),
        PATH=f"{fake_bin}:{env['PATH']}",
        usage_machine="lab",
        usage_promote="true",
        usage_ubuntu="noble",
    )

    result = subprocess.run(["bash", str(PUBLISH_QEMU_SH)], cwd=REPO_ROOT, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        "run packer:init",
        "run packer:build lab --ubuntu noble",
        "run packer:upload-s3 lab --ubuntu noble --bucket homelab-ci-images --region eu-central-1 --architecture x86_64 --promote",
    ]


def test_publish_qemu_threads_arm_store_options(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    log = tmp_path / "mise.log"
    _executable(
        fake_bin / "mise",
        '#!/bin/sh\nset -eu\nprintf "%s\\n" "$*" >>"$MISE_TEST_LOG"\n',
    )
    env = dict(os.environ)
    env.update(
        MISE_TEST_LOG=str(log),
        PATH=f"{fake_bin}:{env['PATH']}",
        usage_architecture="aarch64",
        usage_bucket="homelab-ci-arm-images-eu-west-1",
        usage_machine="box",
        usage_promote="true",
        usage_region="eu-west-1",
        usage_ubuntu="noble",
    )

    result = subprocess.run(["bash", str(PUBLISH_QEMU_SH)], cwd=REPO_ROOT, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        "run packer:init",
        "run packer:build box --ubuntu noble --upstream",
        (
            "run packer:upload-s3 box --ubuntu noble --bucket homelab-ci-arm-images-eu-west-1 "
            "--region eu-west-1 --architecture aarch64 --promote"
        ),
    ]


def test_publish_qemu_hydrates_exact_arm_box_before_box_deps(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    log = tmp_path / "mise.log"
    env_log = tmp_path / "environment.log"
    _executable(
        fake_bin / "mise",
        "#!/bin/sh\n"
        "set -eu\n"
        'printf "%s\\n" "$*" >>"$MISE_TEST_LOG"\n'
        'case "$*" in\n'
        '"run test:build_box_deps"*)\n'
        '  printf "%s|%s|%s|%s|%s|%s\\n" "$HOMELAB_TEST_IN_AWS" '
        '"$HOMELAB_TEST_AWS_COMPUTE_REGION" "$HOMELAB_TEST_AWS_ECR_REGION" '
        '"$HOMELAB_BOX_BASE_BUILD_ID" "$HOMELAB_BOX_BASE_SOURCE_SHA" '
        '"$HOMELAB_BOX_BASE_ARCHITECTURE" >"$ENV_TEST_LOG"\n'
        "  ;;\n"
        "esac\n",
    )
    env = dict(os.environ)
    env.update(
        CI_COMMIT_SHA="d" * 40,
        ENV_TEST_LOG=str(env_log),
        MISE_TEST_LOG=str(log),
        PATH=f"{fake_bin}:{env['PATH']}",
        usage_architecture="aarch64",
        usage_base_build_id="123.arm-box-noble",
        usage_bucket="homelab-ci-arm-images-eu-west-1",
        usage_build_id="123.arm-box-deps-noble",
        usage_machine="box_deps",
        usage_promote="true",
        usage_region="eu-west-1",
        usage_ubuntu="noble",
    )

    result = subprocess.run(["bash", str(PUBLISH_QEMU_SH)], cwd=REPO_ROOT, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        (
            "run ci:hydrate-qemu-images box --ubuntu noble --bucket homelab-ci-arm-images-eu-west-1 "
            "--region eu-west-1 --architecture aarch64 --build-id 123.arm-box-noble"
        ),
        "run test:build_box_deps --ubuntu noble",
        (
            "run packer:upload-s3 box_deps --ubuntu noble --bucket homelab-ci-arm-images-eu-west-1 "
            "--region eu-west-1 --architecture aarch64 --build-id 123.arm-box-deps-noble --promote"
        ),
    ]
    assert env_log.read_text().strip() == f"true|eu-west-1|eu-west-1|123.arm-box-noble|{'d' * 40}|aarch64"


def test_publish_qemu_refuses_arm_box_deps_without_exact_base(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    _executable(fake_bin / "mise", "#!/bin/sh\nset -eu\nexit 99\n")
    env = dict(os.environ)
    env.update(
        CI_COMMIT_SHA="d" * 40,
        PATH=f"{fake_bin}:{env['PATH']}",
        usage_architecture="aarch64",
        usage_machine="box_deps",
        usage_ubuntu="noble",
    )

    result = subprocess.run(["bash", str(PUBLISH_QEMU_SH)], cwd=REPO_ROOT, env=env, text=True, capture_output=True)

    assert result.returncode == 2
    assert "--base-build-id is required" in result.stderr


def test_upload_qemu_stages_bundle_beside_artifacts(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    tar_log = tmp_path / "tar.log"
    artifacts = tmp_path / "scratch" / "noble" / "lab"
    artifacts.mkdir(parents=True)
    (artifacts / "packer-ubuntu-1.raw").write_bytes(b"disk")
    (artifacts / "efivars.fd").write_bytes(b"efi")
    _executable(
        fake_bin / "tar",
        "#!/bin/sh\n"
        "set -eu\n"
        'if [ "${1:-}" = "--help" ]; then printf "%s\\n" "--sparse --zstd"; exit 0; fi\n'
        'while [ "$1" != "-cf" ]; do shift; done\n'
        "shift\n"
        'printf bundle >"$1"\n'
        'printf "%s\\n" "$1" >"$TAR_TEST_LOG"\n',
    )
    _executable(
        fake_bin / "aws",
        '#!/bin/sh\nset -eu\ncase "$*" in *"s3api head-object"*) exit 1 ;; esac\n',
    )
    env = dict(os.environ)
    env.update(
        PATH=f"{fake_bin}:{env['PATH']}",
        TAR_TEST_LOG=str(tar_log),
    )

    result = subprocess.run(
        [
            str(REPO_ROOT / "mise-tasks" / "packer" / "upload-s3.py"),
            "lab",
            "--ubuntu",
            "noble",
            "--artifact-dir",
            str(artifacts),
            "--build-id",
            "test-build",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    bundle = Path(tar_log.read_text().strip())
    assert bundle.parent.parent == artifacts.parent
    assert not bundle.exists()


def test_qemu_build_uploads_only_required_role_files() -> None:
    template = QEMU_TEMPLATE.read_text()

    expected = {
        "roles/boot/files/modules_most",
        "roles/console/files/console-setup",
        "roles/console/files/keyboard",
        "roles/refind/files/zz-stage-efi-stub",
    }
    uploaded_role_files = {match.group(1) for match in re.finditer(r'"\$\{path\.cwd\}/(roles/[^"\n]+)"', template)}
    assert uploaded_role_files == expected
    assert "homelab-source.tar" not in template


def test_qemu_build_uses_one_install_target() -> None:
    template = QEMU_TEMPLATE.read_text()
    provision = QEMU_PROVISION_SH.read_text()

    assert re.search(r'"INSTALL_TARGET"\s+=\s+source\.name == "hetzner" \? "hetzner" : "qemu"', template)
    assert 'export INSTALL_TARGET="${INSTALL_TARGET:-bare_metal}"' in provision
    assert "IMAGE_TARGET" not in template
    assert "QEMU_TEST_IMAGE" not in template


def test_qemu_build_separates_host_os_from_architecture() -> None:
    template = QEMU_TEMPLATE.read_text()

    assert 'data "external-raw" "host_arch"' in template
    assert 'data "external-raw" "host_os"' in template
    assert re.search(r"accelerator\s+= local\.host_os_cfg\.accelerator", template)
    assert re.search(r"format\s+= local\.host_os_cfg\.image_format", template)
    assert 'cloud_image_suffix = "arm64"' in template
    assert 'upstream_archive   = "http://ports.ubuntu.com/ubuntu-ports"' in template


def test_qemu_build_passes_shared_aarch64_firmware_override() -> None:
    template = QEMU_TEMPLATE.read_text()
    build = BUILD_SH.read_text()

    assert 'variable "aarch64_firmware_dir"' in template
    assert 'code = "${local.aarch64_firmware_dir}/edk2-aarch64-code.fd"' in template
    assert 'vars = "${local.aarch64_firmware_dir}/edk2-aarch64-vars.fd"' in template
    assert '"HOMELAB_AARCH64_FIRMWARE_DIR=${local.aarch64_firmware_dir}"' in template
    assert 'aarch64_firmware_dir="${HOMELAB_AARCH64_FIRMWARE_DIR:-${repo_root}/test/firmware}"' in build
    assert '-var "aarch64_firmware_dir=${aarch64_firmware_dir}"' in build


def test_aarch64_firmware_pin_covers_package_code_and_vars() -> None:
    versions = yaml.safe_load((REPO_ROOT / "group_vars" / "all" / "versions.yml").read_text())
    artifact = versions["qemu_efi_aarch64_artifact"]
    script = FIRMWARE_SH.read_text()

    assert artifact == {
        "url": "https://snapshot.debian.org/file/137d1a34bd9ec2e10b1d81331e92d163824579c4",
        "sha256": "95388b7606e821dd8af1dd852767094d569ff78cb2e8f1dc218b60959a52ee81",
    }
    assert "AAVMF_CODE.no-secboot.fd" in script
    assert "AAVMF_VARS.fd" in script


def test_qemu_host_uses_canonical_mise_upstream() -> None:
    provision = QEMU_HOST_PROVISION_SH.read_text()

    assert "https://mise.en.dev/gpg-key.pub" in provision
    assert "https://mise.en.dev/deb stable main" in provision
    assert "mise.jdx.dev" not in provision


def test_qemu_host_retains_caches_without_a_shared_virtualenv() -> None:
    provision = QEMU_HOST_PROVISION_SH.read_text()

    assert "MISE_DATA_DIR=/opt/mise" in provision
    assert "UV_CACHE_DIR=/opt/uv-cache" in provision
    assert "mise exec -- uv sync --locked --link-mode hardlink" in provision
    assert "/opt/venv" not in provision


def test_qemu_host_arm_provisioning_uses_pinned_firmware_and_reduced_toolset() -> None:
    template = QEMU_HOST_TEMPLATE.read_text()
    provision = QEMU_HOST_PROVISION_SH.read_text()

    assert 'qemu_packages        = "qemu-system-arm qemu-efi-aarch64"' in template
    assert 'qemu_system_binary   = "qemu-system-aarch64"' in template
    assert "runner_artifact      = local.versions.gitlab_runner_archive.aarch64" in template
    assert "firmware_url         = local.versions.qemu_efi_aarch64_artifact.url" in template
    assert "firmware_sha256      = local.versions.qemu_efi_aarch64_artifact.sha256" in template
    assert 'firmware_destination = "/opt/homelab-ci/qemu-firmware/aarch64"' in template
    assert 'mise_disable_tools   = "aqua:Kampfkarren/selene"' in template

    assert 'echo "${AARCH64_FIRMWARE_SHA256}  ${firmware_deb}" | sha256sum -c -' in provision
    assert "AAVMF_CODE.no-secboot.fd" in provision
    assert "AAVMF_VARS.fd" in provision
    assert 'MISE_DISABLE_TOOLS="$MISE_DISABLE_TOOLS"' in provision
    assert 'disable_tools = ["%s"]' in provision
    assert "mise exec -- true" in provision
    # Cells hydrate through their checked-out source tree. The AMI build must
    # not try to execute a partial copy of that task without its imports.
    assert "hydrate-qemu-images.py" not in template
    assert "homelab_ci_hydrate_images" not in provision
    assert "mise run ci:hydrate-qemu-images" not in provision
    assert "command -v __QEMU_SYSTEM_BINARY__" in provision
    assert "gitlab_runner_fleeting_arm.pub" in template
    assert "/etc/ssh/authorized_keys/ubuntu" in provision
    assert "sshd -t" in provision


@pytest.mark.parametrize(
    ("ram_gib", "swap_gib"),
    [
        (16, 16),
        (32, 16),
        (128, 32),
    ],
)
def test_qemu_host_scratch_swap_scales_with_memory(tmp_path: Path, ram_gib: int, swap_gib: int) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal:       {ram_gib * 1024 * 1024} kB\n")

    result = subprocess.run(
        ["bash", str(QEMU_HOST_SCRATCH_SH), "--calculate-swap-gib", str(meminfo)],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(swap_gib)


@pytest.mark.parametrize("memtotal", ["", "MemTotal: unknown kB\n", "MemTotal: 0 kB\n", "MemTotal: 1024 bytes\n"])
def test_qemu_host_scratch_swap_rejects_malformed_memory(tmp_path: Path, memtotal: str) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(memtotal)

    result = subprocess.run(
        ["bash", str(QEMU_HOST_SCRATCH_SH), "--calculate-swap-gib", str(meminfo)],
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "invalid MemTotal" in result.stderr


def test_qemu_host_scratch_uses_every_instance_store_device_in_raid0() -> None:
    script = QEMU_HOST_SCRATCH_SH.read_text()

    assert "mapfile -t devs" in script
    assert "lsblk -dn -o NAME,MODEL" in script
    assert "'/Instance Storage/" in script
    assert "--level=0" in script
    assert '--raid-devices="${#devs[@]}" "${devs[@]}"' in script
    assert "Before=multi-user.target" in QEMU_HOST_PROVISION_SH.read_text()


def test_qemu_host_ami_filter_tracks_the_selected_release() -> None:
    template = QEMU_HOST_TEMPLATE.read_text()

    assert 'ubuntu_catalog = yamldecode(file("${path.cwd}/data/ubuntu_releases.yml"))' in template
    assert "ubuntu_version = local.ubuntu_catalog.releases[var.ubuntu_name].version" in template
    assert (
        "ubuntu-${var.ubuntu_name}-${local.ubuntu_version}-${local.architecture_config.ami_architecture}-server-*"
        in template
    )


def test_qemu_host_arm_bake_selects_region_architecture_and_candidate_path(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    packer_log = tmp_path / "packer.log"
    _executable(
        fake_bin / "packer",
        "#!/bin/sh\n"
        "set -eu\n"
        'printf "%s\\n" "$*" >"$PACKER_TEST_LOG"\n'
        "manifest=\n"
        "previous=\n"
        'for argument in "$@"; do\n'
        '  if [ "$previous" = "-var" ]; then\n'
        '    case "$argument" in qemu_host_manifest_path=*) manifest=${argument#*=} ;; esac\n'
        "  fi\n"
        "  previous=$argument\n"
        "done\n"
        'printf \'{"builds":[{"artifact_id":"eu-west-1:ami-1234abcd"}]}\\n\' >"$manifest"\n',
    )
    env = dict(os.environ)
    env.pop("CI", None)
    env.update(
        PATH=f"{fake_bin}:{env['PATH']}",
        PACKER_TEST_LOG=str(packer_log),
        usage_architecture="aarch64",
        usage_promote="false",
        usage_ubuntu="noble",
    )

    result = subprocess.run(
        ["bash", str(QEMU_HOST_AMI_SH)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    call = packer_log.read_text()
    assert "aws_region=eu-west-1" in call
    assert "architecture=aarch64" in call
    assert "Candidate AMI: ami-1234abcd" in result.stdout
    assert "/homelab-ci/ami/qemu-host/aarch64/noble" in result.stdout


def test_qemu_host_rejects_unknown_architecture() -> None:
    env = dict(os.environ, usage_architecture="sparc")

    result = subprocess.run(
        ["bash", str(QEMU_HOST_AMI_SH)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 2
    assert "unsupported architecture sparc" in result.stderr


def test_qemu_host_retention_keeps_legacy_x86_images_in_scope() -> None:
    script = QEMU_HOST_AMI_SH.read_text()

    assert (
        'if [ "$architecture" = aarch64 ]; then\n    image_filters+=("Name=tag:architecture,Values=${architecture}")'
        in script
    )


def test_qemu_cloud_images_use_immutable_release_builds() -> None:
    catalog = yaml.safe_load(UBUNTU_CATALOG.read_text())
    template = QEMU_TEMPLATE.read_text()

    for release in catalog["releases"].values():
        assert set(release) == {"version", "image_release"}
        assert re.fullmatch(r"\d{8}(?:\.\d+)?", release["image_release"])
    assert "release-${local.ubuntu_release.image_release}" in template
    assert 'releases/${local.ubuntu_name}/release"' not in template


def test_ubuntu_completions_use_release_catalog() -> None:
    completion = "yq -r '.releases | keys | .[]' data/ubuntu_releases.yml"

    for task in UBUNTU_COMPLETION_TASKS:
        content = task.read_text()
        assert completion in content
        assert "printf 'noble\\nresolute\\n'" not in content

    assert "noble | resolute)" not in QEMU_HOST_AMI_SH.read_text()


def test_python_task_usage_headers_are_visible_to_mise() -> None:
    for task in PYTHON_USAGE_TASKS:
        content = task.read_text()
        assert "\n#MISE description=" in content
        assert "\n#USAGE arg " in content
        assert "\n# MISE " not in content
        assert "\n# USAGE " not in content


def test_qemu_postprocess_bounds_boot_verification() -> None:
    postprocess = QEMU_POSTPROCESS_SH.read_text()

    assert "timeout --kill-after=30s 300" in postprocess
    assert '"$script_dir/../../test/launch.py"' in postprocess
    assert "--timeout 300" not in postprocess


def test_hetzner_bulk_ssh_isolates_the_compressed_stream(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    log = tmp_path / "ssh.log"
    _executable(
        fake_bin / "ssh",
        '#!/bin/sh\nset -eu\nprintf \'<call>\\n\' >>"$SSH_TEST_LOG"\nprintf \'%s\\n\' "$@" >>"$SSH_TEST_LOG"\n',
    )
    env = dict(os.environ)
    env.update(PATH=f"{fake_bin}:{env['PATH']}", SSH_TEST_LOG=str(log))
    script = f"""
set -euo pipefail
source {shlex.quote(str(HETZNER_SH))}
KEY=/tmp/test_key
KNOWN=/tmp/test_known_hosts
RESCUE_IP=192.0.2.1
ssh_rescue true
ssh_rescue_bulk receive-image
"""

    result = subprocess.run(["bash", "-c", script], cwd=REPO_ROOT, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    calls = [call.splitlines() for call in log.read_text().split("<call>\n") if call]
    assert len(calls) == 2
    control, bulk = calls
    # The bulk variant alone must bypass the user ssh config and prefer the
    # hardware AES cipher, with the remote command still last.
    assert "-F" not in control
    assert bulk[bulk.index("-F") + 1] == "none"
    assert "Ciphers=^aes128-gcm@openssh.com" in bulk
    assert bulk[-2:] == ["root@192.0.2.1", "receive-image"]


@pytest.mark.parametrize(("success_after", "expected_rc", "expected_attempts"), [(3, 0, 3), (41, 1, 40)])
def test_hetzner_rescue_ssh_wait_is_bounded(
    success_after: int,
    expected_rc: int,
    expected_attempts: int,
) -> None:
    script = (
        f"source {shlex.quote(str(HETZNER_SH))}\n"
        + """
sleep() { :; }
attempts=0
ssh_rescue() {
  attempts=$((attempts + 1))
  [ "$attempts" -ge "$SUCCESS_AFTER" ]
}
if wait_for_rescue_sshd; then
  rc=0
else
  rc=$?
fi
printf '%s %s\n' "$rc" "$attempts"
"""
    )
    env = dict(os.environ, SUCCESS_AFTER=str(success_after))

    result = subprocess.run(["bash", "-c", script], cwd=REPO_ROOT, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{expected_rc} {expected_attempts}"
