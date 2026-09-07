#!/usr/bin/env python3
# [MISE] description="Verify notes/ paths cited from code point at existing files"
"""Check bare notes/ paths mentioned in non-Markdown tracked files.

Comments and docstrings cite design notes by repo-root-relative path
(`# See notes/archive/foo.md.`). Nothing else validates those: lint:md-links
covers Markdown *link syntax* inside *.md and resolves relative to the citing
file, while these are bare paths in .py/.yml/.tf/.sh/.j2 resolved from the repo
root. Archiving or renaming a note silently breaks every one of them -- twelve
had rotted before this check existed. Keep the two linters separate; they share
neither the file set, the extraction, nor the resolution base.

Only git-tracked files are scanned, which excludes the gitignored test/out
transcripts and the ha_gui_config clone by construction.
"""

import re
import subprocess
import sys
from pathlib import Path

# Repo-root-relative notes path. The lookbehind stops `roles/x/notes/y.md` from
# matching at the inner `notes/`, which would resolve against the wrong root.
NOTES_REF_RE = re.compile(r"(?<![\w/.-])notes/[A-Za-z0-9_./-]+\.md")


def main():
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())

    # notes/ is a gitignored private clone, absent in CI and in any checkout
    # that did not populate it. Skip rather than fail the public repo's lint.
    if not (root / "notes").is_dir():
        print("lint:notes-refs: skipped, notes/ clone absent")
        return

    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root, text=True).split("\0")
    total = 0
    for rel in sorted(p for p in tracked if p and not p.endswith(".md")):
        path = root / rel
        if path.is_symlink() or not path.is_file():
            continue
        blob = path.read_bytes()
        if b"\0" in blob:  # binary; no citations to find
            continue
        for lineno, line in enumerate(blob.decode("utf-8", errors="replace").splitlines(), 1):
            for ref in NOTES_REF_RE.findall(line):
                if not (root / ref).exists():
                    print(f"{rel}:{lineno}: notes reference does not exist: {ref}", file=sys.stderr)
                    total += 1

    if total:
        print(f"{total} broken notes reference(s)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
