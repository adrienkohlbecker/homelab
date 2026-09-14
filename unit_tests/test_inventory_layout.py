from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def inventory_sections(path: str) -> list[str]:
    return [
        line[1:-1] for line in (ROOT / path).read_text().splitlines() if line.startswith("[") and line.endswith("]")
    ]


def test_inventory_groups_only_own_environment_or_host_variables() -> None:
    assert inventory_sections("hosts.ini") == [
        "physical_lab",
        "physical_pug",
        "physical_fox",
        "prod",
        "prod:children",
        "storage_lab",
        "storage_pug",
    ]
    assert inventory_sections("test/inventory.ini") == [
        "test",
        "storage_lab",
        "storage_pug",
    ]


def test_site_plays_target_hosts_directly() -> None:
    plays = yaml.safe_load((ROOT / "site.yml").read_text())
    assert [play["hosts"] for play in plays] == [
        "lab,pug,fox",
        "fox,lab",
        "lab",
        "lab,pug",
        "lab,pug,fox",
        "lab",
        "lab",
        "lab",
        "udm",
        "lab,pug,fox",
        "lab,pug,fox",
    ]
