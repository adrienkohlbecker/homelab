#!/usr/bin/env python3
import os
import sys
import tempfile

USAGE = "USAGE:\n\tsort_ini.py file.ini"


def sort_ini(fname):
    """sort .ini file: sorts sections and in each section sorts keys"""
    try:
        with open(fname) as f:
            original = f.read()
    except FileNotFoundError:
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

    if sorted_output == original:
        return

    dirn = os.path.dirname(fname)
    fd, tmp = tempfile.mkstemp(dir=dirn)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(sorted_output)
        os.replace(tmp, fname)
    except BaseException:
        os.unlink(tmp)
        raise


if len(sys.argv) < 2:
    print(USAGE, file=sys.stderr)
    sys.exit(1)
else:
    sort_ini(sys.argv[1])
