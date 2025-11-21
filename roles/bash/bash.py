from __future__ import annotations

from pathlib import Path

from pyinfra_roles.helpers import file_put_with_validation

ROLE_ROOT = Path(__file__).resolve().parents[0]
_ROLE_FILE = str(ROLE_ROOT / "files" / "functions.sh")

def apply() -> None:
    file_put_with_validation(
        remote_path="/usr/local/lib/functions.sh",
        local_path=_ROLE_FILE,
        user="root",
        group="root",
        mode="0644",
        validate="bash -n",
    )
