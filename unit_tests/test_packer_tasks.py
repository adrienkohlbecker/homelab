"""Behavioral tests for the Packer mise task wrappers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_SH = REPO_ROOT / "mise-tasks" / "packer" / "build.sh"
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
    """A cache recorded against the build VM's device names fails its import
    and falls back to a device scan on every fixture boot.
    """
    provision = QEMU_PROVISION_SH.read_text()

    assert "zpool import -d /dev/disk/by-partuuid -N" in provision


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


def test_noble_refind_pin_covers_every_architecture_and_is_baked_only_on_noble() -> None:
    versions = yaml.safe_load((REPO_ROOT / "group_vars" / "all" / "versions.yml").read_text())
    pins = versions["refind_noble_release"]
    template = QEMU_TEMPLATE.read_text()
    chroot = QEMU_CHROOT_SH.read_text()

    assert set(pins) == {"x86_64", "aarch64"}
    for architecture, package in {"x86_64": "amd64", "aarch64": "arm64"}.items():
        assert pins[architecture]["url"].endswith(f"refind_0.14.2-2.1_{package}.deb")
        assert re.fullmatch(r"[0-9a-f]{64}", pins[architecture]["sha256"])

    # Only Noble bakes the pin; later releases keep the distribution package.
    assert 'local.ubuntu_name == "noble" ? local.versions.refind_noble_release[local.arch].url : ""' in template
    assert 'local.ubuntu_name == "noble" ? local.versions.refind_noble_release[local.arch].sha256 : ""' in template
    assert "sha256sum -c -" in chroot.split('if [ -n "${REFIND_DEB_URL:-}" ]', 1)[1].split("refind-install", 1)[0]


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


def test_qemu_host_arm_provisioning_uses_packaged_firmware() -> None:
    template = QEMU_HOST_TEMPLATE.read_text()
    provision = QEMU_HOST_PROVISION_SH.read_text()

    assert re.search(r'qemu_packages\s+= "qemu-system-arm qemu-efi-aarch64"', template)
    assert re.search(r'qemu_system_binary\s+= "qemu-system-aarch64"', template)
    assert re.search(r"runner_artifact\s+= local\.versions\.gitlab_runner_archive\.aarch64", template)

    # The firmware comes from the qemu-efi-aarch64 package, not a fetched pin.
    assert "/usr/share/AAVMF/AAVMF_CODE.fd" in provision
    assert "HOMELAB_AARCH64_FIRMWARE_DIR" not in template + provision
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
    # Stock HVF cannot bring a kexec'd kernel's secondary CPUs online, so a Mac
    # builder verifies on one vCPU unless it runs the patched qemu-hvf build.
    assert 'if [ "$(uname -s)" = "Darwin" ] && [[ "$(command -v qemu-system-aarch64)" != */qemu-hvf/* ]]' in postprocess
    assert "vcpus_args=(--vcpus 1)" in postprocess
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
    exercises every module import at load time.
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
    script = tmp_path / "hydrate-qemu-images.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "{lab,pug}" in result.stdout


def test_qemu_host_prehydrate_runs_on_the_toolchain_python_and_is_bounded() -> None:
    """The hydrate script needs 3.14, which the distro's /usr/bin/python3 is not.

    Type=exec is not bounded by TimeoutStartSec once the process runs, so a
    stalled download needs RuntimeMaxSec to release the hydrate lock.
    """
    provision = QEMU_HOST_PROVISION_SH.read_text()
    unit = re.search(r"homelab-ci-prehydrate\.service >/dev/null <<UNIT\n(.*?)\nUNIT\n", provision, re.S)
    assert unit

    (exec_start,) = re.findall(r"^ExecStart=(.*)$", unit.group(1), re.M)
    assert exec_start.startswith("/usr/bin/mise exec -- python3 ")
    assert "RuntimeMaxSec=" in unit.group(1)
    assert not re.search(r"^TimeoutStartSec=", unit.group(1), re.M)
    assert "sys.version_info >= (3, 14)" in QEMU_HOST_SMOKE_SH.read_text()


def test_minimal_fixture_mirrors_journal_with_the_same_unit_as_packer_fixtures() -> None:
    """The stock cloud image has no baked-in mirror, so cloud-init installs it."""
    chroot = QEMU_CHROOT_SH.read_text()
    match = re.search(r"<<'UNIT' >/etc/systemd/system/homelab_guest_journal\.service\n(.*?)\nUNIT\n", chroot, re.S)
    assert match

    user_data = yaml.safe_load((REPO_ROOT / "test" / "minimal" / "user-data").read_text())
    (unit,) = user_data["write_files"]

    assert unit["path"] == "/etc/systemd/system/homelab_guest_journal.service"
    assert unit["content"].rstrip("\n") == match.group(1)
    # StartLimitIntervalSec is a [Unit] key; in [Service] systemd only warns,
    # which fails every later `systemd-analyze verify` on the fixture.
    assert unit["content"].index("StartLimitIntervalSec=0") < unit["content"].index("\n[Service]\n")
    assert ["systemctl", "enable", "--now", "homelab_guest_journal.service"] in user_data["runcmd"]
    # --now: a getty that already started would otherwise keep writing its
    # banner into the write-only journal artifact.
    assert ["systemctl", "mask", "--now", "serial-getty@hvc0.service"] in user_data["runcmd"]
