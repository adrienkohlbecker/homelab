#!/usr/bin/env -S uv run
"""
Maintain the `.ansible-mitogen-strategy` symlink at the repo root.

ansible.cfg points strategy_plugins at this stable path so mitogen survives
Python version bumps in the venv. The symlink target is computed by
importing ansible_mitogen, which fails loudly if mitogen isn't installed.

Imported by machine.py for the test harness; runnable on its own when a
user wants to repair the symlink without invoking the test harness (e.g.
after `uv sync` upgraded Python and ansible.cfg started rejecting plays
with "Invalid play strategy specified: mitogen_linear").
"""

import os
import sys
from pathlib import Path

SYMLINK_NAME = ".ansible-mitogen-strategy"


def ensure_mitogen_symlink(repo_root: Path | None = None) -> Path:
    """Point `<repo>/.ansible-mitogen-strategy` at the live ansible_mitogen plugin dir."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    import ansible_mitogen  # imported lazily so module import doesn't require it

    target = Path(ansible_mitogen.__file__).resolve().parent / "plugins" / "strategy"
    if not target.is_dir():
        raise RuntimeError(
            f"ansible_mitogen is installed but {target} is missing -- mitogen package layout changed?"
        )

    link = repo_root / SYMLINK_NAME
    # readlink() races on concurrent runs; the unlink+symlink dance below
    # is atomic enough for our use (single-host repo) and idempotent.
    current: str | None = None
    if link.is_symlink():
        current = os.readlink(link)
    if current == str(target):
        return link

    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(target)
    return link


def main() -> int:
    link = ensure_mitogen_symlink()
    print(f"{link} -> {os.readlink(link)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
