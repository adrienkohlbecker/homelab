import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def load_identities() -> dict:
    with (ROOT / "group_vars/all/identities.yml").open() as stream:
        return yaml.safe_load(stream)


def service_user_callers() -> set[str]:
    names = set()
    pattern = re.compile(r"service_user_args:\s*\n\s+name:\s+([a-z0-9_-]+)")
    for path in (ROOT / "roles").glob("*/tasks/*.yml"):
        if path.name == "_verify.yml":
            continue
        names.update(pattern.findall(path.read_text()))
    return names


def test_every_service_user_has_one_static_id() -> None:
    identities = load_identities()
    service_ids = identities["static_service_ids"]

    assert set(service_ids) == service_user_callers()
    assert len(service_ids.values()) == len(set(service_ids.values()))
    assert all(60706 <= value <= 60799 for value in service_ids.values())


def test_static_id_namespaces_do_not_collide() -> None:
    identities = load_identities()
    service_ids = identities["static_service_ids"]
    legacy_ids = identities["legacy_service_ids"]
    group_ids = identities["static_group_ids"]
    human_ids = identities["static_human_ids"]

    reserved_identity_values = list(service_ids.values()) + list(legacy_ids.values()) + [human_ids["spouse"]]
    assert len(reserved_identity_values) == len(set(reserved_identity_values))
    assert all(60706 <= value <= 60799 for value in reserved_identity_values)
    assert len(group_ids.values()) == len(set(group_ids.values()))
    assert all(60800 <= value <= 60899 for value in group_ids.values())


def test_human_ids_are_explicit() -> None:
    assert load_identities()["static_human_ids"] == {
        "operator": 1000,
        "spouse": 60740,
    }
