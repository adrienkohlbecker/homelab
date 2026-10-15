"""Tests for test/keep_pools_test.py."""

import sys

import keep_pools_test
import pytest


def test_single_release_catalog_reports_no_release_to_rebuild_from(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(keep_pools_test, "OTHER_UBUNTUS", ())
    monkeypatch.setattr(sys, "argv", ["keep_pools_test.py"])

    with pytest.raises(SystemExit) as exc:
        keep_pools_test.parse_args()

    assert exc.value.code == 2
    assert "no release besides the default" in capsys.readouterr().err
