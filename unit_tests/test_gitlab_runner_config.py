"""Validate the coupled GitLab Runner and AWS profile configuration."""

import configparser
import tomllib
from pathlib import Path

import jinja2
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ROLE = REPO_ROOT / "roles" / "gitlab_runner"


def _runner_values() -> dict:
    values = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    values.update(
        inventory_hostname="fox",
        service_ports={"gitlab_runner": 9252},
        gitlab_runner_shell_enabled=False,
        gitlab_runner_concurrent=134,
        gitlab_runner_aws_qemu_enabled=True,
        gitlab_runner_aws_qemu_token="x86-token",
        gitlab_runner_aws_qemu_capacity_per_instance=13,
        gitlab_runner_aws_qemu_max_instances=5,
        gitlab_runner_aws_qemu_idle_time="2m",
        gitlab_runner_aws_qemu_arm_enabled=True,
        gitlab_runner_aws_qemu_arm_token="arm-token",
        gitlab_runner_aws_qemu_arm_capacity_per_instance=52,
        gitlab_runner_aws_qemu_arm_max_instances=1,
        gitlab_runner_aws_qemu_arm_idle_time="2m",
        gitlab_runner_aws_qemu_arm_ssh_private_key="arm-private-key",
    )
    return values


def _render_template(source: str, values: dict) -> str:
    return jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(source).render(values)


def test_all_aws_runner_pools_render_consistently() -> None:
    rendered = _render_template((ROLE / "templates" / "config.toml.j2").read_text(), _runner_values())
    config = tomllib.loads(rendered)

    assert config["concurrent"] == 134
    runners = {runner["name"]: runner for runner in config["runners"]}
    assert set(runners) == {
        "fox-aws-shell-qemu",
        "fox-aws-shell-qemu-site",
        "fox-aws-shell-qemu-arm",
    }

    expected = {
        "fox-aws-shell-qemu": ("homelab-ci-qemu-host", "homelab-ci-fleeting", 65, 13, 5, "10m0s"),
        "fox-aws-shell-qemu-site": ("homelab-ci-qemu-site", "homelab-ci-fleeting", 1, 1, 1, "10m0s"),
        "fox-aws-shell-qemu-arm": ("homelab-ci-qemu-arm", "homelab-ci-fleeting-arm", 52, 52, 1, "20m0s"),
    }
    for name, (asg, profile, limit, capacity, max_instances, acquire_timeout) in expected.items():
        runner = runners[name]
        autoscaler = runner["autoscaler"]
        assert runner["limit"] == limit
        assert autoscaler["capacity_per_instance"] == capacity
        assert autoscaler["max_instances"] == max_instances
        assert autoscaler["instance_acquire_timeout"] == acquire_timeout
        assert autoscaler["plugin_config"]["name"] == asg
        assert autoscaler["plugin_config"]["profile"] == profile
        assert autoscaler["connector_config"]["timeout"] == "2m0s"

    for name in ("fox-aws-shell-qemu", "fox-aws-shell-qemu-site"):
        assert "key_path" not in runners[name]["autoscaler"]["connector_config"]
        assert "use_static_credentials" not in runners[name]["autoscaler"]["connector_config"]
    arm_connector = runners["fox-aws-shell-qemu-arm"]["autoscaler"]["connector_config"]
    assert arm_connector["key_path"] == "/mnt/services/gitlab_runner/.ssh/fleeting_arm"
    assert arm_connector["use_static_credentials"] is True


def test_aws_profiles_share_credentials_without_source_profile() -> None:
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
    profile_task = next(
        task for task in tasks[0]["block"] if task["name"] == "Render AWS qemu runner credential profile"
    )
    rendered_files = {}
    for task in profile_task["block"]:
        if "copy" not in task:
            continue
        rendered_files[Path(task["copy"]["dest"]).name] = _render_template(task["copy"]["content"], _runner_values())

    config = configparser.ConfigParser()
    config.read_string(rendered_files["config"])
    assert config.sections() == ["profile homelab-ci-fleeting", "profile homelab-ci-fleeting-arm"]
    assert config["profile homelab-ci-fleeting"]["region"] == "eu-central-1"
    assert config["profile homelab-ci-fleeting-arm"]["region"] == "eu-west-1"

    credentials = configparser.ConfigParser()
    credentials.read_string(rendered_files["credentials"])
    assert credentials.sections() == ["homelab-ci-fleeting", "homelab-ci-fleeting-arm"]
    for section in credentials.sections():
        assert credentials[section]["aws_access_key_id"] == _runner_values()["gitlab_runner_aws_qemu_access_key_id"]
        assert (
            credentials[section]["aws_secret_access_key"]
            == _runner_values()["gitlab_runner_aws_qemu_secret_access_key"]
        )
        assert "source_profile" not in credentials[section]
