"""Validate the coupled GitLab Runner and AWS profile configuration."""

import configparser
import tomllib
from pathlib import Path

import hcl2
import jinja2
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ROLE = REPO_ROOT / "roles" / "gitlab_runner"


def _runner_values() -> dict:
    values = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    # Distinct capacity/instance counts per pool so a swapped or mis-derived
    # limit cannot coincidentally match.
    values.update(
        inventory_hostname="fox",
        service_ports={"gitlab_runner": 9252},
        gitlab_runner_shell_enabled=False,
        gitlab_runner_aws_qemu_enabled=True,
        gitlab_runner_aws_qemu_token="x86-token",
        gitlab_runner_aws_qemu_asg_name="x86-asg",
        gitlab_runner_aws_qemu_capacity_per_instance=3,
        gitlab_runner_aws_qemu_max_instances=7,
        gitlab_runner_aws_qemu_idle_time="11m",
        gitlab_runner_aws_qemu_connector_timeout="21s",
        gitlab_runner_aws_qemu_instance_acquire_timeout="31m",
        gitlab_runner_aws_qemu_arm_enabled=True,
        gitlab_runner_aws_qemu_arm_token="arm-token",
        gitlab_runner_aws_qemu_arm_asg_name="arm-asg",
        gitlab_runner_aws_qemu_arm_capacity_per_instance=5,
        gitlab_runner_aws_qemu_arm_max_instances=11,
        gitlab_runner_aws_qemu_arm_idle_time="12m",
        gitlab_runner_aws_qemu_arm_connector_timeout="22s",
        gitlab_runner_aws_qemu_arm_instance_acquire_timeout="32m",
        gitlab_runner_aws_qemu_arm_ssh_private_key="arm-private-key",
    )
    return values


def _render_template(source: str, values: dict) -> str:
    return jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(source).render(values)


def test_runner_limit_is_the_pool_slot_total() -> None:
    rendered = _render_template((ROLE / "templates" / "config.toml.j2").read_text(), _runner_values())
    runners = tomllib.loads(rendered)["runners"]

    assert {runner["name"] for runner in runners} == {"fox-aws-shell-qemu", "fox-aws-shell-qemu-arm"}
    for runner in runners:
        autoscaler = runner["autoscaler"]
        assert runner["limit"] == autoscaler["capacity_per_instance"] * autoscaler["max_instances"]


def test_each_runner_takes_its_own_pools_settings() -> None:
    """Every pool-specific value is distinct, so swapped macro arguments fail here."""
    rendered = _render_template((ROLE / "templates" / "config.toml.j2").read_text(), _runner_values())
    runners = {runner["name"]: runner for runner in tomllib.loads(rendered)["runners"]}

    expected = {
        "fox-aws-shell-qemu": ("x86-token", "x86-asg", 3, 7, "11m", "21s", "31m"),
        "fox-aws-shell-qemu-arm": ("arm-token", "arm-asg", 5, 11, "12m", "22s", "32m"),
    }
    for name, (token, asg, capacity, max_instances, idle, connector_timeout, acquire) in expected.items():
        runner = runners[name]
        autoscaler = runner["autoscaler"]
        assert runner["token"] == token
        assert autoscaler["plugin_config"]["name"] == asg
        assert autoscaler["capacity_per_instance"] == capacity
        assert autoscaler["max_instances"] == max_instances
        assert autoscaler["policy"][0]["idle_time"] == idle
        assert autoscaler["connector_config"]["timeout"] == connector_timeout
        assert autoscaler["instance_acquire_timeout"] == acquire


def test_only_the_arm_runner_uses_a_static_ssh_key() -> None:
    rendered = _render_template((ROLE / "templates" / "config.toml.j2").read_text(), _runner_values())
    connectors = {
        runner["name"]: runner["autoscaler"]["connector_config"] for runner in tomllib.loads(rendered)["runners"]
    }

    assert "key_path" not in connectors["fox-aws-shell-qemu"]
    assert "use_static_credentials" not in connectors["fox-aws-shell-qemu"]
    assert connectors["fox-aws-shell-qemu-arm"]["use_static_credentials"] is True
    assert connectors["fox-aws-shell-qemu-arm"]["key_path"]


def test_fox_runner_pools_match_their_terraform_asgs() -> None:
    # fleeting-plugin-aws treats max_instances as capacity it may request; a
    # runner maximum above the ASG max_size leaves phantom slots after every
    # unfulfilled scale-up. BaseLoader keeps the !vault values opaque.
    values = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    values.update(yaml.load((REPO_ROOT / "group_vars" / "physical_fox.yml").read_text(), Loader=yaml.BaseLoader))
    with (REPO_ROOT / "terraform" / "aws_ci.tf").open() as tf:
        terraform = hcl2.load(tf, serialization_options=hcl2.SerializationOptions(strip_string_quotes=True))
    pools = next(block["ci_qemu_pools"] for block in terraform["locals"] if "ci_qemu_pools" in block)
    runner_prefixes = {"role": "gitlab_runner_aws_qemu", "arm": "gitlab_runner_aws_qemu_arm"}

    assert set(pools) == set(runner_prefixes)
    for pool, prefix in runner_prefixes.items():
        assert values[f"{prefix}_asg_name"] == pools[pool]["name"]
        assert int(values[f"{prefix}_max_instances"]) == pools[pool]["max_size"]


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
    credentials = configparser.ConfigParser()
    credentials.read_string(rendered_files["credentials"])
    assert config.sections() == ["profile homelab-ci-fleeting"]
    assert credentials.sections() == ["homelab-ci-fleeting"]
    # aws-sdk-go-v2 rejects source_profile without role_arn.
    assert "source_profile" not in credentials["homelab-ci-fleeting"]


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
