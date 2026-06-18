"""Tests for Ec2Machine subnet ordering and the boot() AZ-capacity fallback.

boot() launches a single CI cell; when the seeded AZ is out of capacity
(InsufficientInstanceCapacity) it rotates to the next subnet rather than
failing the cell. These tests drive boot() with a fake _call so no AWS
call is made, asserting the rotation order, the fallback, and that
unrelated errors still surface immediately.
"""

import asyncio
from types import SimpleNamespace

import pytest

import machine


class _FakeClientError(Exception):
    """Stand-in for botocore ClientError carrying the .response[Error][Code]."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def _make_ec2(workdir_name: str, call) -> machine.Ec2Machine:
    """Build a bare Ec2Machine with just the attributes boot() reads.

    object.__new__ skips Machine.__init__ (TemporaryDirectory, boto clients)
    so the test stays hermetic; *call* replaces the off-thread boto wrapper.
    """
    m = machine.Ec2Machine.__new__(machine.Ec2Machine)
    m.workdir = SimpleNamespace(name=workdir_name)
    m.machine = "box"
    m.ubuntu_name = "jammy"
    m.role = "testrole"
    m.machine_timeout = 300
    m.instance_id = None
    m._expires_display = ""
    m._client_error = _FakeClientError
    m._call = call
    armed: list[str] = []

    async def _arm_backstop(expires_expr: str) -> None:
        armed.append(expires_expr)

    m._arm_backstop = _arm_backstop
    m._armed = armed
    return m


def _describe_response(subnets: list[str]) -> dict:
    return {"Subnets": [{"SubnetId": s} for s in subnets]}


def test_pick_subnets_rotates_full_set() -> None:
    subnets = ["subnet-c", "subnet-a", "subnet-b"]

    async def call(service, op, *, log=None, **kwargs):
        assert op == "describe_subnets"
        return _describe_response(subnets)

    m = _make_ec2("run-deadbeef", call)
    ordered = asyncio.run(m._pick_subnets())

    # Every subnet present exactly once (a rotation), sorted-then-rotated so
    # the primary pick leads and the rest follow in subnet order.
    assert sorted(ordered) == sorted(subnets)
    assert len(ordered) == len(set(ordered)) == 3
    seed = sum(b"run-deadbeef") % 3
    assert ordered == sorted(subnets)[seed:] + sorted(subnets)[:seed]


def test_boot_falls_back_to_next_az_on_capacity() -> None:
    subnets = ["subnet-a", "subnet-b", "subnet-c"]
    attempts: list[dict] = []

    async def call(service, op, *, log=None, **kwargs):
        if op == "describe_subnets":
            return _describe_response(subnets)
        assert op == "run_instances"
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise _FakeClientError("InsufficientInstanceCapacity")
        return {"Instances": [{"InstanceId": "i-0abc123"}]}

    m = _make_ec2("run-deadbeef", call)
    asyncio.run(m.boot())

    assert m.instance_id == "i-0abc123"
    assert len(attempts) == 2
    # Distinct subnets tried, and the client token is bound to each.
    first, second = attempts
    assert first["SubnetId"] != second["SubnetId"]
    assert first["ClientToken"] != second["ClientToken"]
    assert first["SubnetId"].removeprefix("subnet-") in first["ClientToken"]
    assert m._armed == [m._expires_display.removesuffix("Z")]


def test_boot_reraises_non_capacity_error_without_fallback() -> None:
    subnets = ["subnet-a", "subnet-b", "subnet-c"]
    attempts: list[dict] = []

    async def call(service, op, *, log=None, **kwargs):
        if op == "describe_subnets":
            return _describe_response(subnets)
        attempts.append(kwargs)
        raise _FakeClientError("UnauthorizedOperation")

    m = _make_ec2("run-deadbeef", call)
    with pytest.raises(_FakeClientError) as exc:
        asyncio.run(m.boot())

    assert exc.value.response["Error"]["Code"] == "UnauthorizedOperation"
    # A non-capacity error recurs in every AZ, so only the first is tried.
    assert len(attempts) == 1


def test_boot_classifies_all_az_capacity_exhaustion_as_spot() -> None:
    subnets = ["subnet-a", "subnet-b", "subnet-c"]
    attempts: list[dict] = []

    async def call(service, op, *, log=None, **kwargs):
        if op == "describe_subnets":
            return _describe_response(subnets)
        attempts.append(kwargs)
        raise _FakeClientError("InsufficientInstanceCapacity")

    m = _make_ec2("run-deadbeef", call)
    # A wide concurrent launch can drain every AZ at once; that is transient
    # infra noise, so boot() re-raises it as a SpotInterruptedException, which
    # testrole.py maps to exit 86 and the GitLab job retries once. A raw
    # ClientError would exit 1 and the retry rule (exit_codes: [86]) would miss
    # it.
    with pytest.raises(machine.SpotInterruptedException):
        asyncio.run(m.boot())

    # Exhausted all AZs before giving up.
    assert len(attempts) == len(subnets)
    assert m.instance_id is None
