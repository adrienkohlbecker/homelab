"""Keep Packer's bootstrap rEFInd menu aligned with the Ansible template."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKER_CHROOT = REPO_ROOT / "packer" / "scripts" / "chroot.sh"
REFIND_TEMPLATE = REPO_ROOT / "roles" / "refind" / "templates" / "refind.conf.j2"


def _block_span(text: str, header: str) -> tuple[int, int]:
    start = text.index(header)
    brace = text.index("{", start)
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return start, index + 1
    raise AssertionError(f"Unterminated rEFInd block: {header}")


def _menuentry(text: str, name: str) -> str:
    start, end = _block_span(text, f'menuentry "{name}"')
    return text[start:end]


def _without_block(text: str, header: str) -> str:
    start, end = _block_span(text, header)
    return text[:start] + text[end:]


def _canonical_menuentry(block: str) -> tuple[str, ...]:
    if 'submenuentry "Show ZFSBootMenu (backup)"' in block:
        block = _without_block(block, 'submenuentry "Show ZFSBootMenu (backup)"')

    replacements = {
        "$ZBM_CMDLINE $COMMANDLINE": "<launch_cmdline>",
        "{{ _zbm_launch_cmdline }}": "<launch_cmdline>",
        "${ZBM_KERNEL}": "vmlinux-bootmenu",
        "${UBUNTU_NAME}": "<release>",
        "{{ ansible_distribution_release }}": "<release>",
        "$COMMANDLINE": "<pool_cmdline>",
        "{{ refind_pool_cmdline.stdout }}": "<pool_cmdline>",
    }
    for source, replacement in replacements.items():
        block = block.replace(source, replacement)

    return tuple(
        re.sub(r"\s+", " ", line.strip())
        for line in block.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "icon "))
    )


@pytest.mark.parametrize(
    "name",
    [
        "Ubuntu (ZBM)",
        "Ubuntu (ZBM, Components)",
        "Ubuntu (Linux EFI Stub)",
    ],
)
def test_packer_and_ansible_refind_menuentries_match(name: str) -> None:
    packer = PACKER_CHROOT.read_text()
    ansible = REFIND_TEMPLATE.read_text()

    assert _canonical_menuentry(_menuentry(packer, name)) == _canonical_menuentry(_menuentry(ansible, name))
