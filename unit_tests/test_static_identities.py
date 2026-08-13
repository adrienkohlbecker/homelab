from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def load_identities() -> dict:
    with (ROOT / "group_vars/all/identities.yml").open() as stream:
        return yaml.safe_load(stream)


def role_task_files() -> list[Path]:
    # Skip test scaffolding (_setup.yml, _verify.yml): fixture callers use the
    # test-only id override and must not force a registry entry.
    return [path for path in (ROOT / "roles").glob("*/tasks/*.yml") if not path.name.startswith("_")]


def collect_service_user_args(node: object) -> list[dict]:
    """Every service_user_args dict in a parsed task file, at any nesting."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "service_user_args" and isinstance(value, dict):
                found.append(value)
            else:
                found.extend(collect_service_user_args(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(collect_service_user_args(item))
    return found


def service_user_callers() -> set[str]:
    names = set()
    for path in role_task_files():
        for args in collect_service_user_args(yaml.safe_load(path.read_text())):
            name = args.get("name")
            assert isinstance(name, str), f"{path}: service_user name {name!r} is not a string"
            assert "{" not in name, f"{path}: service_user name {name!r} is not a literal"
            assert "id" not in args, f"{path}: production callers resolve ids from static_service_ids, not id:"
            names.add(name)
    return names


def test_every_service_user_has_one_static_id() -> None:
    assert set(load_identities()["static_service_ids"]) == service_user_callers()


def test_every_static_group_id_has_a_consumer() -> None:
    tasks_text = "".join(path.read_text() for path in role_task_files())
    for key in load_identities()["static_group_ids"]:
        assert f"static_group_ids.{key}" in tasks_text, f"no role consumes static_group_ids.{key}"


def test_static_id_namespaces_do_not_collide() -> None:
    identities = load_identities()
    service_ids = identities["static_service_ids"]
    legacy_ids = identities["legacy_service_ids"]
    group_ids = identities["static_group_ids"]
    human_ids = identities["static_human_ids"]

    assert not set(service_ids) & set(legacy_ids)
    reserved_identity_values = list(service_ids.values()) + list(legacy_ids.values()) + [human_ids["spouse"]]
    assert len(reserved_identity_values) == len(set(reserved_identity_values))
    assert all(60706 <= value <= 60799 for value in reserved_identity_values)
    assert len(group_ids.values()) == len(set(group_ids.values()))
    assert all(60800 <= value <= 60899 for value in group_ids.values())
