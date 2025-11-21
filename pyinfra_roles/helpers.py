from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pyinfra.api.arguments import AllArguments
from pyinfra.api.state import StateOperationHostData
from pyinfra.context import host
from pyinfra.facts.files import Sha256File
from pyinfra.operations import files, server, python
from pyinfra.api import operation, State, Host
from pyinfra.api.exceptions import PyinfraError
from pyinfra.api import FunctionCommand, operation
from pyinfra.api.util import log_error_or_warning


@operation()
def noop(description : str = ""):
    if description:
        host.noop(description)
    yield from ()


@operation()
def fail(description : str = ""):
    def fn(state : State, host : Host) -> None:
        op_data = state.get_op_data_for_host(host, op_hash)
        global_arguments = op_data.global_arguments

        ignore_errors = global_arguments["_ignore_errors"]
        continue_on_error = global_arguments["_continue_on_error"]
        log_error_or_warning(host, ignore_errors, description, continue_on_error)

    yield (FunctionCommand(lambda state, host: False, (), {}))


def file_put_with_validation(remote_path, local_path, user, group, mode, validate):
    local_hash = sha256sum(local_path)
    remote_hash = host.get_fact(Sha256File, remote_path)

    should_backup = remote_hash != None
    should_copy = local_hash != remote_hash

    if not should_copy:
        return noop(
            name="Install the file",
            description="file {0} is already uploaded".format(remote_path),
        )

    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    if should_backup:
        server.shell(
            name="Backup the existing file",
            commands=[f"cp {remote_path} {remote_path}.{timestamp}~"],
        )

    files_put: OperationMeta = files.put(
        name="Install the file",
        src=local_path,
        dest=remote_path,
        user=user,
        group=group,
        mode=mode,
    )

    cmd = server.shell(
        name="Validate the file",
        commands=[f"{validate} {remote_path}"],
        _ignore_errors=True,
    )

    if should_backup:
        server.shell(
            name="Restore the backup",
            commands=[f"cp {remote_path}.{timestamp}~ {remote_path}"],
            _if=cmd.did_error,
        )
    else:
        server.shell(
            name="Delete the file",
            commands=[f"rm -f {remote_path}"],
            _if=cmd.did_error,
        )

    fail(
        name="Trigger failure if needed",
        description="file {0} failed to validate".format(remote_path),
        _if=cmd.did_error,
    )

    return files_put


import hashlib


def sha256sum(filename):
    with open(filename, "rb", buffering=0) as f:
        return hashlib.file_digest(f, "sha256").hexdigest()
