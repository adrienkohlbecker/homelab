#!/usr/bin/env python3
# [MISE] description="Verify local markdown links point to existing files"
"""
Checks every local markdown link in git-tracked .md files.
Skips http/https/ftp/mailto URLs and bare anchors.
Strips fenced code blocks and inline code before scanning to avoid
false positives from [label]: ... patterns inside code fences.

Run via `mise run lint:md-links` or bundled into `mise run lint`.
Exits non-zero with a per-file diagnostic on failure.
"""
import re
import subprocess
import sys
from pathlib import Path

SKIP_PREFIXES = ("http://", "https://", "ftp://", "mailto:")
INLINE_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
REF_RE = re.compile(r"^\[[^\]]+\]:\s+(\S+)", re.MULTILINE)
FENCE_RE = re.compile(r"^(`{3,}|~{3,})[^\n]*\n.*?\n\1", re.MULTILINE | re.DOTALL)
INLINE_CODE_RE = re.compile(r"`+.+?`+", re.DOTALL)


def _strip_code(text):
    text = FENCE_RE.sub("", text)
    text = INLINE_CODE_RE.sub("", text)
    return text


def iter_links(text):
    text = _strip_code(text)
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
        if not (path.parent / url_path).resolve().exists():
            errors.append(url_path)
    return errors


def main():
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    files_output = subprocess.check_output(["git", "ls-files"], cwd=root, text=True)
    files = []
    for rel in files_output.splitlines():
        if not rel.endswith(".md"):
            continue
        p = root / rel
        if not p.is_symlink():
            files.append(p)

    total = 0
    for f in sorted(files):
        for link in check_file(f):
            print(f"{f.relative_to(root)}: broken link: {link}", file=sys.stderr)
            total += 1

    if total:
        print(f"{total} broken link(s)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
