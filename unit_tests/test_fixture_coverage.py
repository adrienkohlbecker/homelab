"""Fixture host_vars must keep designating themselves for opt-in _verify coverage.

Several role checks are gated on a flag that defaults to *off*, so a renamed or
deleted host_var silently stops the check from running while the cell stays
green -- the failure mode a hardcoded fixture hostname gate did not
have. Pin the designations here, in the unit_tests job, where they cost no cell.

Each entry is asserted in both directions: the fixture still declares the flag,
and the file that consumes it still mentions it. The second half is what keeps
this from being a copy of lab.yml -- delete the check and the test tells you to
drop the now-dead flag with it.
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]

# (host, var, is_designated, consumer, what the flag turns on)
COVERAGE_FLAGS = [
    (
        "lab",
        "netdata_diskspace_blocklist",
        lambda value: bool(value),
        "roles/netdata/tasks/_verify_full.yml",
        "the muted per-filesystem disk-space override",
    ),
    (
        "lab",
        "netdata_intelgpu_enabled",
        lambda value: value is True,
        "roles/netdata/tasks/configure.yml",
        "the intelgpu collector config render and removal paths",
    ),
    (
        "lab",
        "netdata_packer_process_group_enabled",
        lambda value: value is True,
        "roles/netdata/tasks/apps_groups.yml",
        "the apps_groups.conf stock-merge and removal paths",
    ),
]

IDS = [f"{host}:{var}" for host, var, _, _, _ in COVERAGE_FLAGS]


def load_host_vars(host: str) -> dict:
    with (ROOT / "test" / "host_vars" / f"{host}.yml").open() as stream:
        return yaml.safe_load(stream)


@pytest.mark.parametrize(("host", "var", "is_designated", "consumer", "enables"), COVERAGE_FLAGS, ids=IDS)
def test_fixture_declares_coverage_flag(host, var, is_designated, consumer, enables):
    host_vars = load_host_vars(host)
    assert var in host_vars, f"test/host_vars/{host}.yml must set {var}; without it {consumer} skips {enables}"
    assert is_designated(host_vars[var]), (
        f"test/host_vars/{host}.yml sets {var}={host_vars[var]!r}, which leaves {consumer} skipping {enables}"
    )


@pytest.mark.parametrize(("host", "var", "is_designated", "consumer", "enables"), COVERAGE_FLAGS, ids=IDS)
def test_coverage_flag_still_has_a_consumer(host, var, is_designated, consumer, enables):
    assert var in (ROOT / consumer).read_text(), (
        f"{consumer} no longer reads {var}; drop the flag from test/host_vars/{host}.yml too"
    )
