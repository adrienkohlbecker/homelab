"""ZFSBootMenu installs the bundle members needed by each architecture."""

from pathlib import Path

import jinja2
import yaml


def test_bundle_members_are_architecture_specific() -> None:
    tasks = yaml.safe_load(Path("roles/zfsbootmenu/tasks/main.yml").read_text())
    install = next(task for task in tasks if task.get("name") == "Install ZBM artifacts")
    members = jinja2.Template(install["loop"])

    common = [
        {"src": "zfsbootmenu.EFI", "dest": "VMLINUZ.EFI"},
        {"src": "cmdline", "dest": "cmdline"},
    ]
    arm_only = [
        {"src": "initramfs-bootmenu.img", "dest": "initramfs-bootmenu.img"},
        {"src": "vmlinux-bootmenu", "dest": "vmlinux-bootmenu"},
    ]
    assert yaml.safe_load(members.render(ansible_architecture="x86_64")) == common
    assert yaml.safe_load(members.render(ansible_architecture="aarch64")) == common + arm_only
