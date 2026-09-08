#!/usr/bin/env python3
# [MISE] description="Verify local Markdown links and bare notes/ references"
"""Check git-tracked references that should resolve to local files."""

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

SKIP_SCHEMES = {"http", "https", "ftp", "mailto"}
INLINE_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
REF_RE = re.compile(r"^\[[^\]]+\]:\s+(\S+)", re.MULTILINE)
FENCE_RE = re.compile(r"^(`{3,}|~{3,})[^\n]*\n.*?\n\1", re.MULTILINE | re.DOTALL)
INLINE_CODE_RE = re.compile(r"`+.+?`+", re.DOTALL)
# Repo-root-relative notes path. The lookbehind stops `roles/x/notes/y.md`
# from matching at the inner `notes/`, which would use the wrong base path.
NOTES_REF_RE = re.compile(r"(?<![\w/.-])notes/[A-Za-z0-9_./-]+\.md")


def iter_links(text):
    text = INLINE_CODE_RE.sub("", FENCE_RE.sub("", text))
    for m in INLINE_RE.finditer(text):
        yield m.group(1)
    for m in REF_RE.finditer(text):
        yield m.group(1)


def local_path(raw):
    parsed = urlsplit(raw.strip().strip("<>"))
    if parsed.scheme in SKIP_SCHEMES or not parsed.path:
        return None
    return parsed.path


def main():
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    tracked = sorted(
        p for p in subprocess.check_output(["git", "ls-files", "-z"], cwd=root, text=True).split("\0") if p
    )
    notes_dir = root / "notes"
    notes_root = notes_dir.resolve()
    notes_present = notes_dir.is_dir()

    total = 0
    for rel in tracked:
        path = root / rel
        if path.is_symlink() or not path.is_file():
            continue
        if rel.endswith(".md"):
            for raw in iter_links(path.read_text(encoding="utf-8", errors="replace")):
                url_path = local_path(raw)
                if url_path is None:
                    continue
                target = (path.parent / url_path).resolve()
                if not notes_present and target.is_relative_to(notes_root):
                    continue
                if not target.exists():
                    print(f"{rel}: broken link: {url_path}", file=sys.stderr)
                    total += 1
        elif notes_present:
            blob = path.read_bytes()
            if b"\0" in blob:
                continue
            for lineno, line in enumerate(blob.decode("utf-8", errors="replace").splitlines(), 1):
                for ref in NOTES_REF_RE.findall(line):
                    if not (root / ref).exists():
                        print(f"{rel}:{lineno}: notes reference does not exist: {ref}", file=sys.stderr)
                        total += 1

    if total:
        print(f"{total} broken local reference(s)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
