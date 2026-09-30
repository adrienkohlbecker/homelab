"""Keep the legacy generator limited to the releases that require it."""

from pathlib import Path

import pytest
import yaml
from jinja2 import StrictUndefined
from jinja2.nativetypes import NativeEnvironment


@pytest.mark.parametrize("release", ["jammy", "noble", "resolute", "future"])
@pytest.mark.parametrize("zfs_root", [False, True])
def test_release_provider_contract(release: str, zfs_root: bool) -> None:
    source = yaml.safe_load((Path(__file__).parents[1] / "group_vars/all/main.yml").read_text())
    environment = NativeEnvironment(undefined=StrictUndefined)
    variables = {"ansible_distribution_release": release, "zfs_root": zfs_root}
    for name in (
        "initramfs_use_dracut",
        "initramfs_packages",
        "initramfs_rebuild_command",
        "initramfs_list_command",
    ):
        variables[name] = environment.from_string(source[name]).render(variables)

    if release in {"jammy", "noble"}:
        assert variables["initramfs_use_dracut"] is False
        assert variables["initramfs_packages"] == ["initramfs-tools"]
        assert variables["initramfs_rebuild_command"] == "update-initramfs -u -k all"
        assert variables["initramfs_list_command"] == "lsinitramfs"
    else:
        assert variables["initramfs_use_dracut"] is True
        assert variables["initramfs_packages"] == ["dracut", *(["zfs-dracut"] if zfs_root else [])]
        assert variables["initramfs_rebuild_command"] == "dracut --force --regenerate-all"
        assert variables["initramfs_list_command"] == "lsinitrd"
