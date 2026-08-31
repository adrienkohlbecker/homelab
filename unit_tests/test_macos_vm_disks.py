"""Exercise the service template's disk persistence with real QEMU block I/O."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import jinja2
import pytest
import yaml


@pytest.mark.skipif(not shutil.which("qemu-system-x86_64") or not shutil.which("qemu-img"), reason="requires QEMU")
@pytest.mark.parametrize("install_mode", [False, True])
def test_only_installer_writes_are_disposable(tmp_path, install_mode):
    role = Path(__file__).resolve().parents[1] / "roles/macos_vm"
    values = yaml.safe_load((role / "defaults/main.yml").read_text())
    values.update(
        macos_vm_workdir=str(tmp_path),
        macos_vm_disk_source=str(tmp_path / "disk.raw"),
        macos_vm_resolved_mac="52:54:00:00:00:01",
        macos_vm_install_mode=install_mode,
    )
    unit = (
        jinja2.Environment(undefined=jinja2.StrictUndefined)
        .from_string((role / "templates/macos_vm.service.j2").read_text())
        .render(values)
    )
    drives = []
    for line in unit.splitlines():
        if not line.strip().startswith("-drive ") or "readonly=on" in line:
            continue
        drive = shlex.split(line.rstrip(" \\"))[1]
        if sys.platform == "darwin":
            drive = drive.replace("aio=native", "aio=threads")  # Linux AIO is unavailable on macOS.
        # A machine-less block test has no flash controller; keep its other options.
        drives.extend(["-drive", drive.replace("if=pflash", "if=none,id=FirmwareVars")])
    for name in ["OVMF_VARS.fd", "disk.raw", "BaseSystem.img"]:
        with (tmp_path / name).open("wb") as image:
            image.truncate(1024 * 1024)
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(tmp_path / "OpenCore.qcow2"), "1M"], check=True, capture_output=True
    )
    environment = dict(os.environ)
    for line in unit.splitlines():
        if line.startswith("Environment="):
            key, value = line.removeprefix("Environment=").split("=", 1)
            environment[key] = value

    def qmp(commands):
        requests = [{"execute": "qmp_capabilities"}, *commands, {"execute": "quit"}]
        result = subprocess.run(
            [
                "qemu-system-x86_64",
                "-machine",
                "none",
                "-nodefaults",
                "-display",
                "none",
                "-S",
                "-qmp",
                "stdio",
                *drives,
            ],
            input="".join(json.dumps(request) + "\n" for request in requests),
            capture_output=True,
            text=True,
            env=environment,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        replies = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        assert all("error" not in reply for reply in replies), replies
        return [reply["return"] for reply in replies if "return" in reply][1:-1], result.stdout

    def io(drive, command):
        return {"execute": "human-monitor-command", "arguments": {"command-line": f'qemu-io {drive} "{command}"'}}

    ids = ["FirmwareVars", "OpenCoreBoot", "MacZvol"] + (["InstallMedia"] if install_mode else [])
    replies, output = qmp([{"execute": "query-block"}, *[io(drive, "write -P 90 0 512") for drive in ids]])
    assert output.count("wrote 512/512 bytes") == len(ids), output
    if install_mode:
        installer = next(block["inserted"] for block in replies[0] if block["device"] == "InstallMedia")
        filename = installer["image"]["filename"]
        if filename.startswith("json:"):
            filename = json.loads(filename.removeprefix("json:"))["file"]["filename"]
        assert Path(filename).parent == tmp_path
        assert not Path(filename).exists()
    assert (tmp_path / "BaseSystem.img").read_bytes() == bytes(1024 * 1024)
    _, output = qmp([io(drive, f"read -P {0 if drive == 'InstallMedia' else 90} 0 512") for drive in ids])
    assert output.count("read 512/512 bytes") == len(ids), output
    assert "Pattern verification failed" not in output
