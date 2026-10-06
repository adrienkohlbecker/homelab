"""Check shaped fluent-bit records against verify expectations.

Usage: lnav_match.py SOURCE EXPECTED [UNEXPECTED]

SOURCE is a JSON Lines file, or "-" for stdin. EXPECTED is a JSON list of
matchers that must each match at least one record; UNEXPECTED is an optional
JSON list of matchers that no record may match. A matcher maps dotted record
paths (``fields.source``) to exact values, plus three operators:

- ``_absent``: paths that must not exist;
- ``_present``: paths that must exist, whatever their value;
- ``_contains``: path to a substring, or list of substrings, of its string value.

Exit status is 0 when every expectation holds, 1 while some expected record is
still missing (callers retry this while records arrive), and 2 when an
unexpected record is present, which no amount of waiting fixes.

The fluentbit role passes this file to ``python3 -c`` on the fixture host, so
it must stay standalone and free of Jinja delimiters.
"""

import json
import sys

MISSING = object()


def value_at(record, path):
    value = record
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def contains(actual, needles):
    if isinstance(needles, str):
        needles = [needles]
    return isinstance(actual, str) and all(needle in actual for needle in needles)


def matches(record, matcher):
    for path, wanted in matcher.items():
        if path == "_absent":
            if any(value_at(record, item) is not MISSING for item in wanted):
                return False
        elif path == "_present":
            if any(value_at(record, item) is MISSING for item in wanted):
                return False
        elif path == "_contains":
            if not all(contains(value_at(record, item), needles) for item, needles in wanted.items()):
                return False
        else:
            actual = value_at(record, path)
            if actual is MISSING or actual != wanted:
                return False
    return True


def load_records(lines):
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def check(records, expected, unexpected):
    for matcher in unexpected:
        for record in records:
            if matches(record, matcher):
                print(json.dumps({"unexpected": matcher, "record": record}), file=sys.stderr)
                return 2
    missing = [matcher for matcher in expected if not any(matches(record, matcher) for record in records)]
    if missing:
        print(json.dumps({"unmatched": missing}), file=sys.stderr)
        return 1
    return 0


def main(argv):
    source, expected = argv[0], json.loads(argv[1])
    unexpected = json.loads(argv[2]) if len(argv) > 2 else []
    if source == "-":
        records = load_records(sys.stdin)
        status = check(records, expected, unexpected)
        if status:
            # Fixture output is small; show it so the mismatch is debuggable.
            print(json.dumps({"records": records}), file=sys.stderr)
        return status
    with open(source, encoding="utf-8", errors="replace") as handle:
        return check(load_records(handle), expected, unexpected)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
