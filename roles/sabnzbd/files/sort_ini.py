#!/usr/bin/env python3
import contextlib
import os
import stat
import sys
import tempfile

USAGE = "USAGE:\n\tsort_ini.py file.ini"


def sort_ini(fname):
    """Sort sections and keys in an application-rewritten INI file."""
    fname = os.path.realpath(fname)
    try:
        with open(fname, encoding="utf-8") as f:
            original = f.read()
    except FileNotFoundError:
        print(f"sort_ini: {fname}: not found, skipping", file=sys.stderr)
        return

    lines = original.splitlines()
    section = ""
    subcat = ""
    sections = {}
    for line in lines:
        line = line.strip()
        if line:
            if line.startswith("[["):
                subcat = line
                continue
            if line.startswith("["):
                section = line
                subcat = ""
                continue
            if section not in sections:
                sections[section] = {}
            if subcat not in sections[section]:
                sections[section][subcat] = []
            sections[section][subcat].append(line)

    if not sections:
        return

    parts = []
    keys = sorted(sections.keys())
    for key in keys:
        subsections = sections[key]
        subsection_keys = sorted(subsections.keys())
        if key != "":
            parts.append(key)
        for subsection_key in subsection_keys:
            values = sorted(subsections[subsection_key])
            if subsection_key != "":
                parts.append(subsection_key)
            parts.extend(values)
    sorted_output = "\n".join(parts) + "\n"

    normalized = "\n".join(line.strip() for line in original.splitlines() if line.strip()) + "\n"
    if sorted_output == normalized:
        return

    file_stat = os.stat(fname)
    directory = os.path.dirname(fname)
    fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".sabnzbd_sort_ini_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), stat.S_IMODE(file_stat.st_mode))
            os.fchown(f.fileno(), file_stat.st_uid, file_stat.st_gid)
            f.write(sorted_output)
        os.replace(temp_path, fname)
    except BaseException:
        # Cleanup is safe because the temporary file was never installed.
        with contextlib.suppress(OSError):
            os.unlink(temp_path)
        raise


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(USAGE, file=sys.stderr)
        sys.exit(1)
    sort_ini(sys.argv[1])
