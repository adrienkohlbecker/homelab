from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from io import BytesIO, StringIO
from pathlib import Path
from typing import Generator

from pyinfra.api.host import Host
from pyinfra.api.state import State
from pyinfra.api.operation import operation, OperationMeta
from pyinfra.api.command import PyinfraCommand, FunctionCommand
from pyinfra.api.exceptions import PyinfraError
from pyinfra.api.util import log_error_or_warning
from pyinfra.context import host
from pyinfra.facts.files import Sha256File
from pyinfra.operations import files, server


@operation()
def noop(description: str = "") -> Generator[PyinfraCommand]:
    """
    No-operation that optionally logs a description.

    Args:
        description: Message to log when this operation is executed
    """
    if description:
        host.noop(description)
    yield from ()


@operation()
def fail(description: str = "") -> Generator[PyinfraCommand]:
    """
    Force a failure in the operation chain.

    Respects global error handling settings (_ignore_errors, _continue_on_error)
    and logs the failure appropriately.

    Args:
        description: Error message to log when this operation fails
    """

    def fn(state: State, host: Host) -> bool:
        op_hash = host.executing_op_hash
        if not op_hash:
            raise PyinfraError("No currently executing op")

        op_data = state.get_op_data_for_host(host, op_hash)
        global_arguments = op_data.global_arguments

        ignore_errors = global_arguments["_ignore_errors"]
        continue_on_error = global_arguments["_continue_on_error"]
        log_error_or_warning(host, ignore_errors, description, continue_on_error)

        return False

    yield FunctionCommand(fn, (), {})


def file_put_with_validation(
    remote_path: str,
    user: str,
    group: str,
    mode: str,
    validate: str,
    local_path: str | None = None,
    content: str | None = None,
) -> OperationMeta:
    """
    Upload a file to a remote host with atomic validation and rollback.

    This function provides safe file deployment with the following guarantees:
    1. Only uploads if the file content differs (idempotent)
    2. Creates timestamped backup of existing file before replacement (preserves metadata)
    3. Validates the new file using the provided command
    4. Automatically restores backup if validation fails
    5. Removes newly uploaded file on validation failure if no prior file existed

    Args:
        remote_path: Absolute path where file should be placed on remote host
        user: Owner username for the file
        group: Group name for the file
        mode: File permissions in octal format (e.g., "0644")
        validate: Shell command to validate the file (receives path as argument)
        local_path: Path to local file or file-like object to upload
        content: String content to upload (alternative to local_path)

    Returns:
        The files.put operation metadata, or noop if no change needed

    Raises:
        PyinfraError: If neither local_path nor content is provided
        PyinfraError: If validation fails (after rollback)

    Example:
        file_put_with_validation(
            remote_path="/etc/sudoers.d/myuser",
            content="myuser ALL=(ALL) NOPASSWD:ALL\n",
            user="root",
            group="root",
            mode="0440",
            validate="visudo -cf",
            _sudo=True,
        )

    Note:
        Backup files use the naming pattern: {remote_path}.{timestamp}~
        They are retained for manual cleanup to enable recovery if needed.
    """
    # Normalize input to file-like object
    if local_path:
        source = local_path
    elif content:
        source = StringIO(content)
    else:
        raise PyinfraError("Either local_path or content must be set")

    # Check if upload is necessary
    local_hash = sha256sum(source)
    remote_hash = host.get_fact(Sha256File, remote_path)

    should_backup = remote_hash is not None
    should_copy = local_hash != remote_hash
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

    backup_path = f"{remote_path}.{timestamp}~"

    if should_copy:
        # Create timestamped backup if file exists
        if should_backup:
            server.shell(
                name="Backup the existing file",
                commands=[f"cp -p {remote_path} {backup_path}"],
            )

        # Upload the new file
        files_put = files.put(
            name="Install the file",
            src=source,
            dest=remote_path,
            user=user,
            group=group,
            mode=mode,
        )
    else:
        files_put = noop(
            name="Install the file",
            description=f"file {remote_path} is already uploaded",
        )

    # Validate the file (whether newly uploaded or existing)
    validation = server.shell(
        name="Validate the file",
        commands=[f"{validate} {remote_path}"],
        _ignore_errors=True,
    )

    # Rollback on validation failure (only if we made changes)
    if should_copy:
        if should_backup:
            server.shell(
                name="Restore the backup",
                commands=[f"cp -p {backup_path} {remote_path}"],
                _if=validation.did_error,
            )
        else:
            server.shell(
                name="Delete the file",
                commands=[f"rm -f {remote_path}"],
                _if=validation.did_error,
            )

    # Fail the operation if validation failed
    fail(
        name="Trigger failure if needed",
        description=f"file {remote_path} failed to validate",
        _if=validation.did_error,
    )

    return files_put


def sha256sum(source: str | Path | StringIO) -> str:
    """
    Calculate SHA256 hash of a file or file-like object.

    Args:
        source: File path (str or Path) or file-like object to hash

    Returns:
        Hexadecimal SHA256 hash string
    """

    # Handle file paths
    if isinstance(source, (str, Path)):
        with open(source, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()

    # Handle file-like objects
    elif isinstance(source, (StringIO)):
        f = BytesIO(source.getvalue().encode("utf-8"))
        return hashlib.file_digest(f, "sha256").hexdigest()

    else:
        raise PyinfraError("Unexpected argument source must be path or StringIO")
