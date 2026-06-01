#!/usr/bin/env python3
import os
import stat
import sys
import tempfile

USAGE = "USAGE:\n\tsort_ini.py file.ini"


def sort_ini(fname):
    """sort .ini file: sorts sections and in each section sorts keys.
    Blank lines and comments are discarded; only suitable for app-rewritten configs."""
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
    for k in keys:
        vals = sections[k]
        sks = sorted(vals.keys())
        if k != "":
            parts.append(k)
        for sk in sks:
            subvals = sorted(vals[sk])
            if sk != "":
                parts.append(sk)
            parts.extend(subvals)
    sorted_output = "\n".join(parts) + "\n"

    normalized = "\n".join(line.strip() for line in original.splitlines() if line.strip()) + "\n"
    if sorted_output == normalized:
        return

    st = os.stat(fname)
    dirn = os.path.dirname(fname)
    fd, tmp = tempfile.mkstemp(dir=dirn, prefix=".sort_ini_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), stat.S_IMODE(st.st_mode))
            os.fchown(f.fileno(), st.st_uid, st.st_gid)
            f.write(sorted_output)
        os.replace(tmp, fname)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(USAGE, file=sys.stderr)
        sys.exit(1)
    else:
        sort_ini(sys.argv[1])
