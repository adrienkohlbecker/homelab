import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "roles/static_identity_migration/files/migrate_zfs_delegations.py"
SPEC = importlib.util.spec_from_file_location("migrate_zfs_delegations", SCRIPT)
assert SPEC
assert SPEC.loader
migrate_zfs_delegations = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migrate_zfs_delegations)


def test_parse_legacy_delegations_uses_only_local_dataset_section() -> None:
    output = """\
---- Permissions on rpool/services/sqlite ----------------------------
Local permissions:
\tuser (unknown: 999) hold,release,send
Descendent permissions:
\tuser (unknown: 997) send
---- Permissions on rpool/services -----------------------------------
Local+Descendent permissions:
\tuser (unknown: 999) hold,release,send
"""

    assert migrate_zfs_delegations.parse_legacy_delegations(
        output,
        "rpool/services/sqlite",
        {997, 999},
    ) == [
        migrate_zfs_delegations.LegacyDelegation(
            dataset="rpool/services/sqlite",
            uid=997,
            scope_option="-d",
        ),
        migrate_zfs_delegations.LegacyDelegation(
            dataset="rpool/services/sqlite",
            uid=999,
            scope_option="-l",
        ),
    ]


def test_parse_legacy_delegations_ignores_current_named_user() -> None:
    output = """\
---- Permissions on tank/data ----------------------------------------
Local+Descendent permissions:
\tuser zfs_autobackup hold,release,send
\tuser (unknown: 999) hold,release,send
"""

    assert (
        migrate_zfs_delegations.parse_legacy_delegations(
            output,
            "tank/data",
            {997},
        )
        == []
    )


def test_parse_legacy_delegations_detects_reused_named_uid(monkeypatch) -> None:
    output = """\
---- Permissions on tank/data ----------------------------------------
Local+Descendent permissions:
\tuser replacement_account hold,release,send
"""
    monkeypatch.setattr(
        migrate_zfs_delegations.pwd,
        "getpwnam",
        lambda name: SimpleNamespace(pw_uid=999),
    )

    assert migrate_zfs_delegations.parse_legacy_delegations(
        output,
        "tank/data",
        {999},
    ) == [
        migrate_zfs_delegations.LegacyDelegation(
            dataset="tank/data",
            uid=999,
            scope_option="-ld",
        )
    ]


def test_main_removes_each_delegation_and_reaudits(monkeypatch, capsys) -> None:
    delegation = migrate_zfs_delegations.LegacyDelegation(
        dataset="tank/data",
        uid=999,
        scope_option="-ld",
    )
    audits = iter([[delegation], []])
    commands = []

    def find_legacy_delegations(legacy_uids):
        assert legacy_uids == {999}
        return next(audits)

    monkeypatch.setattr(
        migrate_zfs_delegations,
        "find_legacy_delegations",
        find_legacy_delegations,
    )
    monkeypatch.setattr(
        migrate_zfs_delegations,
        "run_zfs",
        lambda *args: commands.append(args) or "",
    )
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--uids", "999"])

    migrate_zfs_delegations.main()

    assert commands == [("unallow", "-ld", "-u", "999", "tank/data")]
    assert capsys.readouterr().out.endswith("legacy delegations found: 1\n")
