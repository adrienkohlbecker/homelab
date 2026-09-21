"""Behavioral tests for the Packer mise task wrappers."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
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
QEMU_HOST_KERNEL_SH = REPO_ROOT / "packer" / "aws" / "files" / "install_ga_kernel.sh"
QEMU_HOST_SCRATCH_SH = REPO_ROOT / "packer" / "aws" / "files" / "homelab_ci_prepare_scratch.sh"
QEMU_HOST_SMOKE_SH = REPO_ROOT / "packer" / "aws" / "files" / "qemu_host_smoke.sh"
QEMU_POSTPROCESS_SH = REPO_ROOT / "packer" / "scripts" / "postprocess.sh"
QEMU_TEMPLATE = REPO_ROOT / "packer" / "qemu.pkr.hcl"
QEMU_PROVISION_SH = REPO_ROOT / "packer" / "scripts" / "provision.sh"
QEMU_CHROOT_SH = REPO_ROOT / "packer" / "scripts" / "chroot.sh"
UBUNTU_CATALOG = REPO_ROOT / "data" / "ubuntu_releases.yml"
UBUNTU_COMPLETION_TASKS = (
    BUILD_SH,
    REPO_ROOT / "mise-tasks" / "packer" / "hetzner.sh",
    QEMU_HOST_AMI_SH,
    PUBLISH_QEMU_SH,
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
        usage_sources="lab",
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
        assert "-only=qemu.lab" in call
    assert cache_log.read_text().splitlines() == [f"{env['HOMELAB_CI_DIR']}/packer_cache"] * len(ubuntus)


@pytest.mark.parametrize("fixture_machine", ["lab", "pug"])
def test_publish_qemu_builds_and_uploads_promoted_fixture(tmp_path: Path, fixture_machine: str) -> None:
    fake_bin = tmp_path / "bin"
    log = tmp_path / "mise.log"
    _executable(
        fake_bin / "mise",
        '#!/bin/sh\nset -eu\nprintf "%s\\n" "$*" >>"$MISE_TEST_LOG"\n',
    )
    _executable(fake_bin / "uname", "#!/bin/sh\nset -eu\nprintf 'x86_64\\n'\n")
    env = dict(os.environ)
    env.update(
        MISE_TEST_LOG=str(log),
        PATH=f"{fake_bin}:{env['PATH']}",
        usage_machine=fixture_machine,
        usage_promote="true",
        usage_ubuntu="noble",
    )
    env.pop("usage_architecture", None)

    result = subprocess.run(["bash", str(PUBLISH_QEMU_SH)], cwd=REPO_ROOT, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    upload = f"run packer:upload-s3 {fixture_machine} --ubuntu noble --architecture x86_64 --promote"
    assert log.read_text().splitlines() == [
        f"{upload} --preflight",
        "run packer:init",
        f"run packer:build {fixture_machine} --ubuntu noble",
        upload,
    ]


def test_publish_qemu_stops_before_building_when_preflight_fails(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    log = tmp_path / "mise.log"
    _executable(
        fake_bin / "mise",
        '#!/bin/sh\nset -eu\nprintf "%s\\n" "$*" >>"$MISE_TEST_LOG"\ncase "$*" in *--preflight) exit 1 ;; esac\n',
    )
    env = dict(os.environ)
    env.update(
        MISE_TEST_LOG=str(log),
        PATH=f"{fake_bin}:{env['PATH']}",
        usage_architecture="aarch64",
        usage_machine="lab",
        usage_ubuntu="noble",
    )

    result = subprocess.run(["bash", str(PUBLISH_QEMU_SH)], cwd=REPO_ROOT, env=env, text=True, capture_output=True)

    assert result.returncode == 1
    assert len(log.read_text().splitlines()) == 1


@pytest.mark.parametrize(
    ("architecture", "upstream"),
    [(None, False), ("aarch64", True)],
)
def test_publish_qemu_threads_arm_store_options(tmp_path: Path, architecture: str | None, upstream: bool) -> None:
    fake_bin = tmp_path / "bin"
    log = tmp_path / "mise.log"
    _executable(
        fake_bin / "mise",
        '#!/bin/sh\nset -eu\nprintf "%s\\n" "$*" >>"$MISE_TEST_LOG"\n',
    )
    _executable(fake_bin / "uname", "#!/bin/sh\nset -eu\nprintf 'arm64\\n'\n")
    env = dict(os.environ)
    env.update(
        MISE_TEST_LOG=str(log),
        PATH=f"{fake_bin}:{env['PATH']}",
        usage_machine="lab",
        usage_promote="true",
        usage_ubuntu="noble",
        usage_upstream=str(upstream).lower(),
    )
    if architecture is None:
        env.pop("usage_architecture", None)
    else:
        env["usage_architecture"] = architecture

    result = subprocess.run(["bash", str(PUBLISH_QEMU_SH)], cwd=REPO_ROOT, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    upload = "run packer:upload-s3 lab --ubuntu noble --architecture aarch64 --promote"
    assert log.read_text().splitlines() == [
        f"{upload} --preflight",
        "run packer:init",
        "run packer:build lab --ubuntu noble" + (" --upstream" if upstream else ""),
        upload,
    ]


def _upload_fixture(tmp_path: Path, tar_tail: str = "") -> tuple[list[str], dict[str, str], Path, Path, Path]:
    """Fake tar/aws around a lab artifact dir and capture the uploaded stream."""
    fake_bin = tmp_path / "bin"
    tar_log = tmp_path / "tar.log"
    stream_log = tmp_path / "stream.log"
    manifest_log = tmp_path / "manifest.json"
    aws_calls = tmp_path / "aws.calls"
    artifacts = tmp_path / "scratch" / "noble" / "lab"
    artifacts.mkdir(parents=True)
    (artifacts / "packer-ubuntu-1.raw").write_bytes(b"disk")
    (artifacts / "efivars.fd").write_bytes(b"efi")
    _executable(
        fake_bin / "tar",
        "#!/bin/sh\n"
        "set -eu\n"
        'if [ "${1:-}" = "--help" ]; then printf "%s\\n" "--sparse --zstd"; exit 0; fi\n'
        'printf "%s\\n" "$*" >"$TAR_TEST_LOG"\n'
        "printf bundle\n" + tar_tail,
    )
    _executable(
        fake_bin / "aws",
        '#!/bin/sh\nset -eu\nprintf "%s\\n" "$*" >>"$AWS_CALL_LOG"\ncase "$*" in\n'
        '  *"s3api head-object"*) printf "%s\\n" "An error occurred (404) when calling the HeadObject operation" >&2; exit 1 ;;\n'
        '  *"s3 cp - "*)\n'
        '    if [ "${AWS_STREAM_FAIL:-}" = "1" ]; then exit 9; fi\n'
        '    cat >"$AWS_STREAM_LOG" ;;\n'
        '  *"s3api put-object"*)\n'
        '    previous=""\n'
        '    for argument in "$@"; do\n'
        '      if [ "$previous" = "--body" ]; then cp "$argument" "$AWS_MANIFEST_LOG"; fi\n'
        '      previous="$argument"\n'
        "    done ;;\n"
        "esac\n",
    )
    env = dict(os.environ)
    env.update(
        # Pin provenance so a modified developer checkout does not refuse upload.
        CI_COMMIT_SHA="d" * 40,
        PATH=f"{fake_bin}:{env['PATH']}",
        TAR_TEST_LOG=str(tar_log),
        AWS_STREAM_LOG=str(stream_log),
        AWS_MANIFEST_LOG=str(manifest_log),
        AWS_CALL_LOG=str(aws_calls),
    )
    argv = [
        str(REPO_ROOT / "mise-tasks" / "packer" / "upload-s3.py"),
        "lab",
        "--ubuntu",
        "noble",
        "--artifact-dir",
        str(artifacts),
        "--build-id",
        "test-build",
    ]
    return argv, env, artifacts, tar_log, stream_log


def test_upload_qemu_streams_bundle_without_staging(tmp_path: Path) -> None:
    argv, env, artifacts, tar_log, stream_log = _upload_fixture(tmp_path)

    result = subprocess.run(argv, cwd=REPO_ROOT, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    assert "-cf - -C" in tar_log.read_text()
    assert stream_log.read_bytes() == b"bundle"
    assert (
        json.loads(Path(env["AWS_MANIFEST_LOG"]).read_text())["bundle"]["sha256"]
        == hashlib.sha256(b"bundle").hexdigest()
    )
    assert [p.name for p in artifacts.parent.iterdir()] == ["lab"]


def test_upload_qemu_stops_stream_on_sigterm(tmp_path: Path) -> None:
    argv, env, artifacts, _tar_log, _stream_log = _upload_fixture(tmp_path, tar_tail='kill -TERM "$PPID"\n')

    result = subprocess.run(argv, cwd=REPO_ROOT, env=env, text=True, capture_output=True)

    assert result.returncode == 143
    assert [p.name for p in artifacts.parent.iterdir()] == ["lab"]


def test_upload_qemu_does_not_publish_manifest_after_tar_failure(tmp_path: Path) -> None:
    argv, env, artifacts, _tar_log, _stream_log = _upload_fixture(tmp_path, tar_tail="exit 7\n")

    result = subprocess.run(argv, cwd=REPO_ROOT, env=env, text=True, capture_output=True)

    assert result.returncode != 0
    assert "s3api put-object" not in Path(env["AWS_CALL_LOG"]).read_text()
    assert [p.name for p in artifacts.parent.iterdir()] == ["lab"]


def test_upload_qemu_does_not_publish_manifest_after_upload_failure(tmp_path: Path) -> None:
    argv, env, _artifacts, _tar_log, _stream_log = _upload_fixture(tmp_path)
    env["AWS_STREAM_FAIL"] = "1"

    result = subprocess.run(argv, cwd=REPO_ROOT, env=env, text=True, capture_output=True)

    assert result.returncode != 0
    assert "s3api put-object" not in Path(env["AWS_CALL_LOG"]).read_text()


def test_upload_qemu_preflight_rejects_foreign_architecture_without_touching_s3(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    _executable(fake_bin / "aws", "#!/bin/sh\nexit 99\n")
    host = {"arm64": "aarch64"}.get(os.uname().machine, os.uname().machine)
    foreign = "x86_64" if host == "aarch64" else "aarch64"
    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}")

    result = subprocess.run(
        [
            str(REPO_ROOT / "mise-tasks" / "packer" / "upload-s3.py"),
            "lab",
            "--architecture",
            foreign,
            "--preflight",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert f"built on this {host} host as {foreign}" in result.stderr


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


def test_qemu_image_is_sealed_ready_to_boot() -> None:
    """Fixture images must not push per-boot work onto every machine.

    Both cost every cell, every boot: a cache recorded against the build
    VM's device names fails its import and falls back to a device scan, and
    an array sealed mid-resync rebuilds itself in full on each boot.
    """
    provision = QEMU_PROVISION_SH.read_text()

    assert "zpool import -d /dev/disk/by-partuuid -N" in provision
    assert re.search(r"for md in /dev/md/efi /dev/md/swap /dev/md/podman; do", provision)
    assert 'mdadm --wait "$md" || true' in provision


def test_qemu_fixture_mirrors_journal_from_the_first_entry() -> None:
    """The mirror follows the journal rather than using ForwardToConsole.

    Forwarding starts only when journald opens the console and never replays
    the kernel records it imported from kmsg, so the artifact opened mid-boot
    with no kernel lines. --lines=all makes a late start cost nothing, and
    --cursor-file stops a Restart= from replaying the journal twice.
    """
    chroot = QEMU_CHROOT_SH.read_text()

    assert "/etc/systemd/system/homelab_guest_journal.service" in chroot
    assert "--follow --lines=all" in chroot
    assert "--cursor-file=/run/homelab_guest_journal.cursor" in chroot
    assert "StandardOutput=file:/dev/hvc0" in chroot
    # A second writer on the same chardev would double every line.
    assert "ForwardToConsole=" not in chroot
    # Otherwise the generator's console getty interleaves its banners in.
    assert "systemctl mask serial-getty@hvc0.service" in chroot


def test_qemu_build_separates_host_os_from_architecture() -> None:
    template = QEMU_TEMPLATE.read_text()

    assert 'data "external-raw" "host_arch"' in template
    assert 'data "external-raw" "host_os"' in template
    assert re.search(r"accelerator\s+= local\.host_os_cfg\.accelerator", template)
    assert re.search(r"format\s+= local\.host_os_cfg\.image_format", template)
    assert re.search(r'upstream_archive\s+= "http://ports\.ubuntu\.com/ubuntu-ports"', template)


def _ar_member(name: str, data: bytes) -> bytes:
    header = f"{name:<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode()
    return header + data + (b"\n" if len(data) % 2 else b"")


def _edk2_package(tmp_path: Path) -> Path:
    """Build a minimal qemu-efi-aarch64-shaped .deb with both CODE variants."""
    members = {
        "./usr/share/AAVMF/AAVMF_CODE.no-secboot.fd": b"plain code",
        "./usr/share/AAVMF/AAVMF_CODE.secboot.fd": b"secure code",
        "./usr/share/AAVMF/AAVMF_VARS.fd": b"vars template",
    }
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:xz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    package = tmp_path / "qemu-efi-aarch64.deb"
    package.write_bytes(
        b"!<arch>\n" + _ar_member("debian-binary", b"2.0\n") + _ar_member("data.tar.xz", data.getvalue())
    )
    return package


def _firmware_checkout(tmp_path: Path, package: Path, sha256: str) -> Path:
    """Lay out firmware.sh with the pins it reads, as the AMI bake does."""
    root = tmp_path / "checkout"
    (root / "mise-tasks" / "test").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "group_vars" / "all").mkdir(parents=True)
    shutil.copy(FIRMWARE_SH, root / "mise-tasks" / "test" / "firmware.sh")
    shutil.copy(REPO_ROOT / "data" / "architectures.yml", root / "data" / "architectures.yml")
    versions = {
        "qemu_efi_aarch64_version": "test",
        "qemu_efi_aarch64_artifact": {"url": package.as_uri(), "sha256": sha256},
    }
    (root / "group_vars" / "all" / "versions.yml").write_text(yaml.safe_dump(versions))
    return root / "mise-tasks" / "test" / "firmware.sh"


def _run_firmware(script: Path, firmware_dir: Path, host: str = "aarch64") -> subprocess.CompletedProcess[str]:
    fake_bin = script.parents[2] / "bin"
    _executable(fake_bin / "uname", f"#!/bin/sh\nprintf '{host}\\n'\n")
    # firmware.sh parses its pins with python3 + PyYAML; use this interpreter.
    path = f"{fake_bin}:{Path(sys.executable).parent}:{os.environ['PATH']}"
    env = dict(os.environ, HOMELAB_AARCH64_FIRMWARE_DIR=str(firmware_dir), PATH=path)
    return subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True)


def test_firmware_is_not_fetched_on_non_arm_hosts(tmp_path: Path) -> None:
    missing_package = tmp_path / "absent.deb"
    script = _firmware_checkout(tmp_path, missing_package, "0" * 64)
    firmware_dir = tmp_path / "firmware"

    result = _run_firmware(script, firmware_dir, host="x86_64")

    assert result.returncode == 0, result.stderr
    assert "nothing to fetch" in result.stdout
    assert not firmware_dir.exists()


def test_firmware_installs_the_plain_pair_once(tmp_path: Path) -> None:
    package = _edk2_package(tmp_path)
    script = _firmware_checkout(tmp_path, package, hashlib.sha256(package.read_bytes()).hexdigest())
    firmware_dir = tmp_path / "firmware"

    first = _run_firmware(script, firmware_dir)

    assert first.returncode == 0, first.stderr
    assert (firmware_dir / "edk2-aarch64-code.fd").read_bytes() == b"plain code"
    assert (firmware_dir / "edk2-aarch64-vars.fd").read_bytes() == b"vars template"
    assert (firmware_dir / "archive.sha256").read_text().strip() == hashlib.sha256(package.read_bytes()).hexdigest()

    # A matching marker short-circuits before any download.
    package.unlink()
    second = _run_firmware(script, firmware_dir)
    assert second.returncode == 0, second.stderr
    assert "already present" in second.stdout


def test_firmware_rejects_an_archive_that_does_not_match_its_pin(tmp_path: Path) -> None:
    package = _edk2_package(tmp_path)
    script = _firmware_checkout(tmp_path, package, "0" * 64)
    firmware_dir = tmp_path / "firmware"

    result = _run_firmware(script, firmware_dir)

    assert result.returncode == 1
    assert "sha256 mismatch" in result.stderr
    assert not firmware_dir.exists()


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


def test_qemu_host_arm_provisioning_uses_pinned_firmware() -> None:
    template = QEMU_HOST_TEMPLATE.read_text()
    provision = QEMU_HOST_PROVISION_SH.read_text()

    assert 'qemu_packages        = "qemu-system-arm qemu-efi-aarch64"' in template
    assert 'qemu_system_binary   = "qemu-system-aarch64"' in template
    assert "runner_artifact      = local.versions.gitlab_runner_archive.aarch64" in template
    assert 'firmware_destination = "/opt/homelab-ci/qemu-firmware/aarch64"' in template

    # The AMI installs firmware through the same script operators run locally.
    assert '"${path.cwd}/mise-tasks/test/firmware.sh"' in template
    assert 'bash "$firmware_tree/mise-tasks/test/firmware.sh"' in provision
    assert "AAVMF" not in provision
    assert "test -r ${HOMELAB_AARCH64_FIRMWARE_DIR}/archive.sha256" in provision
    assert "mise exec -- true" in provision
    # The boot-time pre-hydration runs a baked copy of the task outside any
    # checkout; test_qemu_host_prehydrate_tree_is_self_contained proves the
    # copy carries its imports. It must not rely on the mise task wrapper.
    assert "homelab_ci_hydrate_images" not in provision
    assert "mise run ci:hydrate-qemu-images" not in provision
    assert "command -v __QEMU_SYSTEM_BINARY__" in provision
    assert "gitlab_runner_fleeting_arm.pub" in template
    assert "/etc/ssh/authorized_keys/ubuntu" in provision
    assert "sshd -t" in provision


@pytest.mark.parametrize(("architecture", "installs_key"), [("x86_64", False), ("aarch64", True)])
def test_qemu_host_static_key_is_arm_only(tmp_path: Path, architecture: str, installs_key: bool) -> None:
    provision = QEMU_HOST_PROVISION_SH.read_text()
    key_section = provision.split("# connector; x86 continues to use EC2 Instance Connect.\n", 1)[1].split(
        "\nsudo install -dm 0755 /opt/mise", 1
    )[0]
    commands = tmp_path / "commands"
    script = f"""set -euo pipefail
sudo() {{ printf '%s\\n' "$*" >> "$COMMANDS"; }}
{key_section}
"""
    subprocess.run(
        ["bash", "-c", script],
        check=True,
        env={**os.environ, "TARGET_ARCHITECTURE": architecture, "COMMANDS": str(commands)},
    )
    assert ("/etc/ssh/authorized_keys/ubuntu" in commands.read_text() if commands.exists() else False) is installs_key


@pytest.mark.skipif(
    shutil.which("qemu-system-aarch64") is None
    or not (REPO_ROOT / "test" / "firmware" / "edk2-aarch64-code.fd").is_file(),
    reason="needs qemu-system-aarch64 and the fetched firmware (mise run test:firmware)",
)
def test_qemu_host_smoke_boots_the_pinned_firmware_to_its_boot_manager() -> None:
    firmware = REPO_ROOT / "test" / "firmware"

    result = subprocess.run(
        [
            "bash",
            str(QEMU_HOST_SMOKE_SH),
            "firmware",
            "qemu-system-aarch64",
            "virt",
            str(firmware / "edk2-aarch64-code.fd"),
            str(firmware / "edk2-aarch64-vars.fd"),
        ],
        text=True,
        capture_output=True,
        timeout=240,
    )

    assert result.returncode == 0, result.stderr
    assert "edk2-aarch64-code.fd reached the UEFI boot manager" in result.stdout


def test_qemu_host_boots_the_ga_kernel_before_anything_is_provisioned() -> None:
    """The kernel swap and its reboot precede every other provisioner.

    linux-aws runs ahead of the release GA kernel the rest of the fleet is on,
    so the toolchain and firmware checks would otherwise vouch for a kernel the
    captured image never boots.
    """
    template = QEMU_HOST_TEMPLATE.read_text()

    kernel_step = template.index('script = "${path.root}/files/install_ga_kernel.sh"')
    reboot_step = template.index('inline            = ["sudo systemctl reboot"]')
    upload_step = template.index('provisioner "file"')
    assert kernel_step < reboot_step < upload_step
    assert "expect_disconnect = true" in template
    # The reboot drops the user-data shutdown schedule (it lives in /run), so
    # the watchdog must be armed again after it, before the long provisioning.
    rearm_step = template.index('inline = ["sudo shutdown -h +180"]')
    assert reboot_step < rearm_step < template.index("provision_qemu_host.sh")

    kernel = QEMU_HOST_KERNEL_SH.read_text()
    # Grub boots the highest version it finds, so the replacement has to be
    # installed and the AWS flavour gone before the reboot, in that order.
    assert kernel.index("apt-get install -y -qq --no-install-recommends linux-generic") < kernel.index("apt-get purge")
    # Otherwise apt resolves linux-image-generic's firmware alternation to the
    # full blob set, none of which an EC2 instance has hardware for.
    assert "linux-generic linux-firmware-minimal" in kernel
    # The kernel being purged is the running one; the prerm defaults to
    # refusing that, and noninteractive dpkg never gets asked.
    assert "linux-base linux-base/removing-running-kernel boolean false" in kernel
    # linux-aws builds the Nitro drivers in and the image boots initrd-less on
    # that; the GA kernel needs an initramfs to find root at all.
    assert "rm -f /etc/default/grub.d/40-force-partuuid.cfg" in kernel
    assert kernel.index("GRUB_FORCE_PARTUUID") < kernel.index("update-grub")


@pytest.mark.parametrize(
    ("running", "installed", "succeeds"),
    [
        ("6.8.0-139-generic", "", True),
        ("7.0.0-1012-aws", "", False),
        ("6.8.0-139-generic", "installed linux-image-7.0.0-1012-aws", False),
    ],
)
def test_qemu_host_smoke_rejects_a_host_that_kept_the_aws_kernel(
    tmp_path: Path, running: str, installed: str, succeeds: bool
) -> None:
    """Both halves matter: the booted kernel and what grub can pick next time.

    A leftover linux-aws package outranks the GA kernel on the next boot, so an
    image that merely happens to be running generic right now is not enough.
    """
    fake_bin = tmp_path / "bin"
    _executable(fake_bin / "uname", f"#!/bin/sh\nset -eu\nprintf '{running}\\n'\n")
    _executable(fake_bin / "dpkg-query", f"#!/bin/sh\nset -eu\nprintf '{installed}\\n'\n")

    result = subprocess.run(
        ["bash", str(QEMU_HOST_SMOKE_SH), "kernel"],
        text=True,
        capture_output=True,
        timeout=60,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert (result.returncode == 0) is succeeds
    if not succeeds:
        assert "qemu_host_smoke:" in result.stderr


def test_qemu_host_smoke_fails_when_qemu_exits_before_the_boot_manager(tmp_path: Path) -> None:
    fake_qemu = tmp_path / "qemu-system-test"
    _executable(fake_qemu, "#!/bin/sh\nexit 1\n")
    variables = tmp_path / "vars.fd"
    variables.write_bytes(b"vars")

    result = subprocess.run(
        [
            "bash",
            str(QEMU_HOST_SMOKE_SH),
            "firmware",
            str(fake_qemu),
            "virt",
            str(tmp_path / "code.fd"),
            str(variables),
        ],
        text=True,
        capture_output=True,
        timeout=60,
    )

    assert result.returncode == 1
    assert "did not reach the UEFI boot manager" in result.stderr


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
    assert "lsblk -dn -o PATH,MODEL" in script
    assert "'/Instance Storage/" in script
    assert "--level=0" in script
    assert '--raid-devices="${#devs[@]}" "${devs[@]}"' in script
    assert "Before=multi-user.target" in QEMU_HOST_PROVISION_SH.read_text()


def test_qemu_host_scratch_uses_one_non_root_ebs_disk() -> None:
    script = QEMU_HOST_SCRATCH_SH.read_text()

    assert "findmnt -n -o SOURCE /" in script
    assert 'lsblk -srdpno NAME,TYPE "$root_source"' in script
    assert "/Elastic Block Store/" in script
    assert 'if [ "$dev" != "$root_disk" ]' in script
    assert "multiple non-root EBS disks are ambiguous" in script


def test_qemu_host_readiness_waits_for_scratch_setup() -> None:
    script = QEMU_HOST_PROVISION_SH.read_text()

    assert "for _ in {1..90}; do" in script
    assert "systemctl is-active homelab-ci-scratch.service" in script
    assert '[ "$scratch_state" = active ]' in script


def test_qemu_host_ami_filter_tracks_the_selected_release() -> None:
    template = QEMU_HOST_TEMPLATE.read_text()

    assert 'ubuntu_catalog = yamldecode(file("${path.cwd}/data/ubuntu_releases.yml"))' in template
    assert "ubuntu_version = local.ubuntu_catalog.releases[var.ubuntu_name].version" in template
    assert (
        "ubuntu-${var.ubuntu_name}-${local.ubuntu_version}-${local.architecture_config.ami_architecture}-server-*"
        in template
    )


def _run_qemu_host_bake(
    tmp_path: Path, *, promote: bool, resolved_ami: str = "ami-1234abcd"
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    """Run an ARM qemu-host bake against fake packer/aws/sleep binaries.

    The fake SSM resolves the newly written parameter version to
    *resolved_ami*, which lets tests model SSM's asynchronous AMI validation.
    """
    fake_bin = tmp_path / "bin"
    packer_log = tmp_path / "packer.log"
    aws_log = tmp_path / "aws.log"
    sleep_log = tmp_path / "sleep.log"
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
        'printf \'{"builds":[{"artifact_id":"eu-central-1:ami-1234abcd"}]}\\n\' >"$manifest"\n',
    )
    _executable(
        fake_bin / "aws",
        "#!/bin/sh\n"
        "set -eu\n"
        'printf "%s\\n" "$*" >>"$AWS_TEST_LOG"\n'
        'case "$*" in\n'
        '*"ssm put-parameter"*) printf "7\\n" ;;\n'
        '*"--name /homelab-ci/ami/qemu-host/aarch64/noble:7 "*) printf "%s\\n" "$RESOLVED_AMI" ;;\n'
        '*"ssm get-parameter"*) printf "ami-promoted\\n" ;;\n'
        '*"ec2 describe-images"*) printf "[]\\n" ;;\n'
        "esac\n",
    )
    _executable(fake_bin / "sleep", '#!/bin/sh\nprintf "%s\\n" "$*" >>"$SLEEP_TEST_LOG"\n')
    env = dict(os.environ)
    env.pop("CI", None)
    env.update(
        AWS_TEST_LOG=str(aws_log),
        PATH=f"{fake_bin}:{env['PATH']}",
        PACKER_TEST_LOG=str(packer_log),
        RESOLVED_AMI=resolved_ami,
        SLEEP_TEST_LOG=str(sleep_log),
        usage_architecture="aarch64",
        usage_promote="true" if promote else "false",
        usage_ubuntu="noble",
    )

    result = subprocess.run(
        ["bash", str(QEMU_HOST_AMI_SH)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    return result, packer_log, aws_log, sleep_log


def test_qemu_host_arm_bake_selects_region_architecture_and_candidate_path(tmp_path: Path) -> None:
    result, packer_log, aws_log, _sleep_log = _run_qemu_host_bake(tmp_path, promote=False)

    assert result.returncode == 0, result.stderr
    call = packer_log.read_text()
    assert "aws_region=" not in call
    assert "architecture=aarch64" in call
    assert "Candidate AMI: ami-1234abcd" in result.stdout
    assert "/homelab-ci/ami/qemu-host/aarch64/noble" in result.stdout
    # Candidate bakes prune too; retention protects the promoted AMI and keeps
    # the newest builds, so unpromoted candidates cannot accumulate.
    assert "Prune: nothing to remove" in result.stdout
    describe = next(line for line in aws_log.read_text().splitlines() if "ec2 describe-images" in line)
    assert "--region eu-central-1" in describe
    assert "Name=tag:architecture,Values=aarch64" in describe


def test_qemu_host_promotion_waits_for_ssm_to_resolve_the_new_version(tmp_path: Path) -> None:
    result, _packer_log, _aws_log, sleep_log = _run_qemu_host_bake(tmp_path, promote=True)

    assert result.returncode == 0, result.stderr
    assert "Promoted /homelab-ci/ami/qemu-host/aarch64/noble -> ami-1234abcd (version 7)" in result.stdout
    # The rollback command must restore the same validated data type.
    assert "--data-type aws:ec2:image --value ami-promoted --overwrite" in result.stdout
    assert not sleep_log.exists()


def test_qemu_host_promotion_fails_when_ssm_never_accepts_the_ami(tmp_path: Path) -> None:
    result, _packer_log, _aws_log, sleep_log = _run_qemu_host_bake(tmp_path, promote=True, resolved_ami="ami-stale")

    assert result.returncode == 1
    assert "never resolved to ami-1234abcd" in result.stderr
    assert "Promoted" not in result.stdout
    assert len(sleep_log.read_text().splitlines()) == 24


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


def test_qemu_host_prehydrate_tree_is_self_contained(tmp_path: Path) -> None:
    """Rebuild the AMI's hydrate tree from provision's installs and run it.

    A hydrate import or data read the AMI does not ship would make the boot
    unit fail on every host; running --help from outside the repository
    exercises every module import and data read at load time.
    """
    template = QEMU_HOST_TEMPLATE.read_text()
    provision = QEMU_HOST_PROVISION_SH.read_text()
    installs = re.findall(r'install -D -m \d+ /tmp/(\S+) "\$hydrate_root/(\S+)"', provision)
    assert installs
    sources = {
        Path(line.split("}/", 1)[1].rstrip('",')).name: line.split("}/", 1)[1].rstrip('",')
        for line in template.splitlines()
        if "${path.cwd}/" in line
    }
    for name, relative in installs:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO_ROOT / sources[name]).read_bytes())
    script = tmp_path / "mise-tasks" / "ci" / "hydrate-qemu-images.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "{lab,pug}" in result.stdout
