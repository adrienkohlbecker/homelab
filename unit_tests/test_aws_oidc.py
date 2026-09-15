"""Behavioral tests for the GitLab OIDC credential helper."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AWS_OIDC_SH = REPO_ROOT / "mise-tasks" / "ci" / "aws-oidc.sh"


def test_token_file_is_private_even_when_a_readable_one_exists(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "mise").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "mise").chmod(0o755)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    stale = checkout / ".aws_web_identity_token"
    stale.write_text("stale")
    stale.chmod(0o644)

    result = subprocess.run(
        ["bash", "-c", f'source "{AWS_OIDC_SH}" arn:aws:iam::000000000000:role/test session'],
        cwd=checkout,
        env=dict(os.environ, GITLAB_OIDC_TOKEN="token-value", PATH=f"{fake_bin}:{os.environ['PATH']}"),
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert stale.read_text() == "token-value"
    assert stat.S_IMODE(stale.stat().st_mode) == 0o600
