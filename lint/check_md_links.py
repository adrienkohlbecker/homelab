#!/usr/bin/env python3
"""Verify that local markdown links resolve to existing files.

Discovers files via git ls-files (default) or find (--use-find).
Skips http/https/ftp/mailto URLs and bare anchors (#...).
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

SKIP_PREFIXES = ("http://", "https://", "ftp://", "mailto:")
INLINE_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
REF_RE = re.compile(r"^\[[^\]]+\]:\s+(\S+)", re.MULTILINE)


def iter_links(text):
    for m in INLINE_RE.finditer(text):
        yield m.group(1)
    for m in REF_RE.finditer(text):
        yield m.group(1)


def check_file(path):
    errors = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for raw in iter_links(text):
        url = raw.strip().strip("<>")
        url_path = url.split("#")[0].strip()
        if not url_path or any(url_path.startswith(p) for p in SKIP_PREFIXES):
            continue
        resolved = (path.parent / url_path).resolve()
        if not resolved.exists():
            errors.append(url_path)
    return errors


def discover_git(root, exclude_dirs):
    out = subprocess.check_output(["git", "ls-files"], cwd=root, text=True)
    files = []
    for rel in out.splitlines():
        if not rel.endswith(".md"):
            continue
        parts = Path(rel).parts
        if any(d in exclude_dirs for d in parts):
            continue
        p = root / rel
        if not p.is_symlink():
            files.append(p)
    return files


_ALWAYS_SKIP = {".git", ".venv", "node_modules", "__pycache__"}


def discover_find(root, exclude_dirs):
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs and d not in _ALWAYS_SKIP]
        for fname in filenames:
            if fname.endswith(".md"):
                p = Path(dirpath) / fname
                if not p.is_symlink():
                    files.append(p)
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--exclude-dir",
        action="append",
        default=[],
        dest="exclude_dirs",
        metavar="DIR",
        help="Skip files under this directory name (repeatable)",
    )
    parser.add_argument(
        "--use-find",
        action="store_true",
        help="Discover files via os.walk instead of git ls-files (includes uncommitted files)",
    )
    args = parser.parse_args()

    root = Path(
        subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
    )

    discover = discover_find if args.use_find else discover_git
    files = sorted(discover(root, set(args.exclude_dirs)))

    total = 0
    for f in files:
        try:
            rel = f.relative_to(root)
        except ValueError:
            rel = f
        for link in check_file(f):
            print(f"{rel}: broken link: {link}", file=sys.stderr)
            total += 1

    if total:
        print(f"{total} broken link(s)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
