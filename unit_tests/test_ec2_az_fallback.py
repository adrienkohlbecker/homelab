"""Tests for Ec2Machine subnet ordering and EC2 Fleet launch behavior.

boot() launches a single CI cell through an instant EC2 Fleet. These tests drive
boot() with a fake _call so no AWS call is made, asserting the subnet rotation,
Fleet request shape, benchmark override, and capacity-error classification.
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


def _fleet_response(instance_id: str = "i-0abc123") -> dict:
    return {
        "fleetId": "fleet-123",
        "fleetInstanceSet": [
            {
                "InstanceIds": [instance_id],
                "InstanceType": "c6a.large",
                "Lifecycle": "spot",
            }
        ],
    }


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


def test_boot_creates_price_capacity_optimized_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(machine.EC2_INSTANCE_TYPE_CANDIDATES, "box", ("c6a.large", "c6i.large"))
    subnets = ["subnet-a", "subnet-b", "subnet-c"]
    attempts: list[dict] = []

    async def call(service, op, *, log=None, **kwargs):
        if op == "describe_subnets":
            return _describe_response(subnets)
        assert op == "create_fleet"
        attempts.append(kwargs)
        return _fleet_response()

    m = _make_ec2("run-deadbeef", call)
    asyncio.run(m.boot())

    assert m.instance_id == "i-0abc123"
    assert len(attempts) == 1
    request = attempts[0]
    assert request["Type"] == "instant"
    assert request["SpotOptions"] == {
        "AllocationStrategy": "price-capacity-optimized",
        "InstanceInterruptionBehavior": "terminate",
    }
    assert request["TargetCapacitySpecification"] == {
        "TotalTargetCapacity": 1,
        "SpotTargetCapacity": 1,
        "DefaultTargetCapacityType": "spot",
    }
    overrides = request["LaunchTemplateConfigs"][0]["Overrides"]
    assert len(overrides) == 6
    assert {o["InstanceType"] for o in overrides} == {"c6a.large", "c6i.large"}
    assert {o["SubnetId"] for o in overrides} == set(subnets)
    assert {o["ImageId"] for o in overrides} == {"resolve:ssm:/homelab-ci/ami/box/jammy"}
    assert {s["ResourceType"] for s in request["TagSpecifications"]} == {"fleet", "instance"}
    assert m._armed == [m._expires_display.removesuffix("Z")]


def test_boot_env_override_restricts_fleet_to_one_instance_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_EC2_INSTANCE_TYPE", "m6i.large")
    monkeypatch.setitem(machine.EC2_INSTANCE_TYPE_CANDIDATES, "box", ("c6a.large", "c6i.large"))
    subnets = ["subnet-a", "subnet-b"]
    attempts: list[dict] = []

    async def call(service, op, *, log=None, **kwargs):
        if op == "describe_subnets":
            return _describe_response(subnets)
        assert op == "create_fleet"
        attempts.append(kwargs)
        return _fleet_response()

    m = _make_ec2("run-deadbeef", call)
    asyncio.run(m.boot())

    overrides = attempts[0]["LaunchTemplateConfigs"][0]["Overrides"]
    assert [o["InstanceType"] for o in overrides] == ["m6i.large", "m6i.large"]


def test_boot_reraises_non_capacity_error_without_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(machine.EC2_INSTANCE_TYPE_CANDIDATES, "box", ("c6a.large", "c6i.large"))
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
    # A request-level API/auth error happens before Fleet can select a pool.
    assert len(attempts) == 1


def test_boot_classifies_fleet_capacity_errors_as_spot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(machine.EC2_INSTANCE_TYPE_CANDIDATES, "box", ("c6a.large", "c6i.large"))
    subnets = ["subnet-a", "subnet-b", "subnet-c"]
    attempts: list[dict] = []

    async def call(service, op, *, log=None, **kwargs):
        if op == "describe_subnets":
            return _describe_response(subnets)
        attempts.append(kwargs)
        return {
            "fleetId": "fleet-123",
            "fleetInstanceSet": [],
            "errorSet": [
                {
                    "ErrorCode": "InsufficientInstanceCapacity",
                    "ErrorMessage": "no spot capacity",
                }
            ],
        }

    m = _make_ec2("run-deadbeef", call)
    # A wide concurrent launch can drain every AZ at once; that is transient
    # infra noise, so boot() re-raises it as a SpotInterruptedException, which
    # testrole.py maps to exit 86 and the GitLab job retries once. A raw
    # ClientError would exit 1 and the retry rule (exit_codes: [86]) would miss
    # it.
    with pytest.raises(machine.SpotInterruptedException):
        asyncio.run(m.boot())

    assert len(attempts) == 1
    assert m.instance_id is None


def test_boot_raises_when_fleet_launches_no_instance_for_non_capacity_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(machine.EC2_INSTANCE_TYPE_CANDIDATES, "box", ("c6a.large",))
    subnets = ["subnet-a"]

    async def call(service, op, *, log=None, **kwargs):
        if op == "describe_subnets":
            return _describe_response(subnets)
        return {
            "fleetId": "fleet-123",
            "fleetInstanceSet": [],
            "errorSet": [{"ErrorCode": "InvalidAMIID.NotFound", "ErrorMessage": "bad image"}],
        }

    m = _make_ec2("run-deadbeef", call)
    with pytest.raises(RuntimeError, match="create-fleet launched 0 instances"):
        asyncio.run(m.boot())
