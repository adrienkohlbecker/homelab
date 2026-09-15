from pathlib import Path

from ansible.inventory.manager import InventoryManager
from ansible.parsing.dataloader import DataLoader

ROOT = Path(__file__).parents[1]


def load_inventory(path: str) -> InventoryManager:
    return InventoryManager(loader=DataLoader(), sources=[str(ROOT / path)])


def test_root_host_vars_never_name_a_fixture_host() -> None:
    # The harness copies host_vars/ beside every test playbook, so a file named
    # after a fixture host would load prod settings into that fixture.
    fixture_hosts = {host.name for host in load_inventory("test/inventory.ini").get_hosts()}
    root_host_vars = {path.stem for path in (ROOT / "host_vars").glob("*.yml")}

    assert not root_host_vars & fixture_hosts


def test_group_vars_files_name_an_inventory_group() -> None:
    # A renamed or dropped group leaves its vars file loading on no host.
    groups = {name for path in ("hosts.ini", "test/inventory.ini") for name in load_inventory(path).groups}
    group_var_files = {path.stem for path in (ROOT / "group_vars").glob("*.yml")}

    assert group_var_files <= groups
