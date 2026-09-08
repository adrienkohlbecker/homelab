"""Shared timeout, signal, and keep-VM lifecycle for harness runs."""

import asyncio
import contextlib
from collections.abc import AsyncIterator

from machine import Machine
from utils import cancel_on_signal, print_line


@contextlib.asynccontextmanager
async def machine_session(machine: Machine, timeout: int) -> AsyncIterator[None]:
    """Run a machine body under the harness timeout and keep-VM policy."""

    task = asyncio.current_task()
    assert task is not None
    timer_absorbed = False

    with cancel_on_signal(task):
        async with asyncio.timeout(timeout) as timeout_cm:
            async with machine:
                try:
                    try:
                        yield
                    except asyncio.CancelledError:
                        if machine.keep_vm and timeout_cm.expired() and task.cancelling():
                            task.uncancel()
                            timer_absorbed = True
                            print_line(f"Timed out after {timeout}s; --keep set, dropping to SSH for debug")
                        else:
                            raise
                finally:
                    if machine.keep_vm and not task.cancelling():
                        with contextlib.suppress(RuntimeError):
                            # A fired deadline cannot be rescheduled, but it is
                            # already spent and no longer bounds the debug wait.
                            timeout_cm.reschedule(None)
                        machine.print_ssh_instructions()
                        await machine.wait()

    if timer_absorbed:
        raise TimeoutError()
