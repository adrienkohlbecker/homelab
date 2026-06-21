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


def iter_links(text):
    text = INLINE_CODE_RE.sub("", FENCE_RE.sub("", text))
    for m in INLINE_RE.finditer(text):
        yield m.group(1)
    for m in REF_RE.finditer(text):
        yield m.group(1)


def main():
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    # The private notes/ clone is absent in CI; skip links into it only then.
    missing_notes_dir = None if (root / "notes").exists() else (root / "notes").resolve()

    total = 0
    for rel in sorted(subprocess.check_output(["git", "ls-files"], cwd=root, text=True).splitlines()):
        path = root / rel
        if not rel.endswith(".md") or path.is_symlink():
            continue
        for raw in iter_links(path.read_text(encoding="utf-8", errors="replace")):
            url_path = raw.strip().strip("<>").split("#")[0].strip()
            if not url_path or any(url_path.startswith(p) for p in SKIP_PREFIXES):
                continue
            target = (path.parent / url_path).resolve()
            if missing_notes_dir is not None and target.is_relative_to(missing_notes_dir):
                continue
            if not target.exists():
                print(f"{path.relative_to(root)}: broken link: {url_path}", file=sys.stderr)
                total += 1

    if total:
        print(f"{total} broken link(s)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
