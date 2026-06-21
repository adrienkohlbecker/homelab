#!/usr/bin/env -S uv run --script
"""Print roles that consume a given helper role."""

import sys

import detect


def main() -> int:
    argv = sys.argv[1:]
    if len(argv) != 1:
        print("usage: ci:role-deps <helper-role-name>", file=sys.stderr)
        return 2
    helper = argv[0]

    for consumer in detect.build_role_deps_map().get(helper, []):
        print(consumer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
