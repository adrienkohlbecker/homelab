"""Exercise the supported vault client command-line forms."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "vault-client.sh"


def run_client(*args):
    environment = dict(os.environ)
    environment.pop("HOMELAB_VAULT_PASSWORD_PROD", None)
    environment.pop("HOMELAB_VAULT_PASSWORD_TEST", None)
    environment.update(
        HOMELAB_VAULT_PASSWORD_PROD="prod-secret",
        HOMELAB_VAULT_PASSWORD_TEST="test-secret",
    )
    return subprocess.run([SCRIPT, *args], capture_output=True, text=True, env=environment)


@pytest.mark.parametrize(
    ("args", "password"),
    [
        ((), "prod-secret"),
        (("prod",), "prod-secret"),
        (("test",), "test-secret"),
        (("--vault-id", "prod"), "prod-secret"),
        (("--vault-id", "test"), "test-secret"),
    ],
)
def test_supported_arguments_select_password(args, password):
    result = run_client(*args)

    assert result.returncode == 0, result.stderr
    assert result.stdout == password


@pytest.mark.parametrize(
    "args",
    [
        ("--vault-id",),
        ("--vault-id=test",),
        ("unknown",),
        ("prod", "test"),
        ("--vault-id", "prod", "extra"),
    ],
)
def test_unsupported_arguments_fail(args):
    result = run_client(*args)

    assert result.returncode == 1
    assert result.stdout == ""
