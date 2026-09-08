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
HETZNER_SH = REPO_ROOT / "mise-tasks" / "packer" / "hetzner.sh"
QEMU_HOST_AMI_SH = REPO_ROOT / "mise-tasks" / "packer" / "qemu-host-ami.sh"
QEMU_HOST_TEMPLATE = REPO_ROOT / "packer" / "aws" / "qemu_host.pkr.hcl"
QEMU_HOST_PROVISION_SH = REPO_ROOT / "packer" / "aws" / "files" / "provision_qemu_host.sh"
QEMU_POSTPROCESS_SH = REPO_ROOT / "packer" / "scripts" / "postprocess.sh"
QEMU_TEMPLATE = REPO_ROOT / "packer" / "qemu.pkr.hcl"
QEMU_PROVISION_SH = REPO_ROOT / "packer" / "scripts" / "provision.sh"
UBUNTU_CATALOG = REPO_ROOT / "data" / "ubuntu_releases.yml"
UBUNTU_COMPLETION_TASKS = (
    BUILD_SH,
    REPO_ROOT / "mise-tasks" / "packer" / "hetzner.sh",
    QEMU_HOST_AMI_SH,
    REPO_ROOT / "mise-tasks" / "packer" / "publish-qemu.sh",
    REPO_ROOT / "mise-tasks" / "test" / "build_box_deps.sh",
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


def test_qemu_host_uses_canonical_mise_upstream() -> None:
    provision = QEMU_HOST_PROVISION_SH.read_text()

    assert "https://mise.en.dev/gpg-key.pub" in provision
    assert "https://mise.en.dev/deb stable main" in provision
    assert "mise.jdx.dev" not in provision


def test_qemu_host_retains_caches_without_a_shared_virtualenv() -> None:
    provision = QEMU_HOST_PROVISION_SH.read_text()

    assert "MISE_DATA_DIR=/opt/mise" in provision
    assert "UV_CACHE_DIR=/opt/uv-cache" in provision
    assert "mise exec -- uv sync --frozen --link-mode hardlink" in provision
    assert "/opt/venv" not in provision


def test_qemu_host_ami_filter_tracks_the_selected_release() -> None:
    template = QEMU_HOST_TEMPLATE.read_text()

    assert 'ubuntu_catalog = yamldecode(file("${path.cwd}/data/ubuntu_releases.yml"))' in template
    assert "ubuntu_version = local.ubuntu_catalog.releases[var.ubuntu_name].version" in template
    assert "ubuntu-${var.ubuntu_name}-${local.ubuntu_version}-amd64-server-*" in template


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
