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
        gitlab_runner_concurrent=160,
        gitlab_runner_aws_qemu_enabled=True,
        gitlab_runner_aws_qemu_token="x86-token",
        gitlab_runner_aws_qemu_capacity_per_instance=10,
        gitlab_runner_aws_qemu_max_instances=6,
        gitlab_runner_aws_qemu_idle_time="2m",
        gitlab_runner_aws_qemu_arm_enabled=True,
        gitlab_runner_aws_qemu_arm_token="arm-token",
        gitlab_runner_aws_qemu_arm_capacity_per_instance=78,
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

    assert config["concurrent"] == 160
    runners = {runner["name"]: runner for runner in config["runners"]}
    assert set(runners) == {
        "fox-aws-shell-qemu",
        "fox-aws-shell-qemu-arm",
    }

    expected = {
        "fox-aws-shell-qemu": ("homelab-ci-qemu-host", "homelab-ci-fleeting", 60, 10, 6, "10m0s"),
        "fox-aws-shell-qemu-arm": ("homelab-ci-qemu-arm", "homelab-ci-fleeting", 78, 78, 1, "20m0s"),
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

    role_connector = runners["fox-aws-shell-qemu"]["autoscaler"]["connector_config"]
    assert "key_path" not in role_connector
    assert "use_static_credentials" not in role_connector
    arm_connector = runners["fox-aws-shell-qemu-arm"]["autoscaler"]["connector_config"]
    assert arm_connector["key_path"] == "/mnt/services/gitlab_runner/.ssh/fleeting_arm"
    assert arm_connector["use_static_credentials"] is True


def test_aws_runners_share_one_credential_profile() -> None:
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
    assert config.sections() == ["profile homelab-ci-fleeting"]
    assert config["profile homelab-ci-fleeting"]["region"] == "eu-central-1"

    credentials = configparser.ConfigParser()
    credentials.read_string(rendered_files["credentials"])
    assert credentials.sections() == ["homelab-ci-fleeting"]
    for section in credentials.sections():
        assert credentials[section]["aws_access_key_id"] == _runner_values()["gitlab_runner_aws_qemu_access_key_id"]
        assert (
            credentials[section]["aws_secret_access_key"]
            == _runner_values()["gitlab_runner_aws_qemu_secret_access_key"]
        )
        assert "source_profile" not in credentials[section]


def test_autoscaler_connector_changes_restart_runner() -> None:
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
    role_tasks = tasks[0]["block"]
    config_task = next(task for task in role_tasks if task["name"] == "Render gitlab-runner config.toml")
    key_block = next(task for task in role_tasks if task["name"] == "Render ARM Fleeting SSH key")
    key_task = next(task for task in key_block["block"] if task["name"] == "Render ARM Fleeting SSH private key")
    unit_task = next(task for task in role_tasks if task["name"] == "Manage gitlab_runner.service")
    restart = unit_task["vars"]["systemd_unit_args"]["restart"]

    assert config_task["register"] == "gitlab_runner_config"
    assert key_task["register"] == "gitlab_runner_aws_qemu_arm_ssh_key"
    assert "gitlab_runner_config.changed" in restart
    assert "gitlab_runner_aws_qemu_arm_ssh_key.changed" in restart


def test_lab_fixture_supplies_secrets_for_enabled_runner_backends() -> None:
    # BaseLoader leaves unrelated !vault values opaque and booleans as strings.
    values = yaml.load((REPO_ROOT / "group_vars" / "test.yml").read_text(), Loader=yaml.BaseLoader)
    values.update(yaml.load((REPO_ROOT / "test" / "host_vars" / "lab.yml").read_text(), Loader=yaml.BaseLoader))
    backend_secrets = {
        "gitlab_runner_shell_enabled": {"gitlab_runner_shell_token"},
        "gitlab_runner_aws_qemu_enabled": {"gitlab_runner_aws_qemu_token"},
        "gitlab_runner_aws_qemu_arm_enabled": {
            "gitlab_runner_aws_qemu_arm_token",
            "gitlab_runner_aws_qemu_arm_ssh_private_key",
        },
    }
    required = {
        secret for backend, secrets in backend_secrets.items() if values.get(backend) == "true" for secret in secrets
    }
    if (
        values.get("gitlab_runner_aws_qemu_enabled") == "true"
        or values.get("gitlab_runner_aws_qemu_arm_enabled") == "true"
    ):
        required.update({"gitlab_runner_aws_qemu_access_key_id", "gitlab_runner_aws_qemu_secret_access_key"})

    assert required
    assert all(isinstance(values.get(secret), str) and values[secret].strip() for secret in required)
