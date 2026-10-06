"""Cross-check fox's GitLab Runner pools against their Terraform ASGs."""

from pathlib import Path

import hcl2
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_fox_runner_pools_match_their_terraform_asgs() -> None:
    # fleeting-plugin-aws treats max_instances as capacity it may request; a
    # runner maximum above the ASG max_size leaves phantom slots after every
    # unfulfilled scale-up. BaseLoader keeps the !vault values opaque.
    fox = yaml.load((REPO_ROOT / "group_vars" / "physical_fox.yml").read_text(), Loader=yaml.BaseLoader)
    with (REPO_ROOT / "terraform" / "aws_ci.tf").open() as tf:
        terraform = hcl2.load(tf, serialization_options=hcl2.SerializationOptions(strip_string_quotes=True))
    asgs = next(block["ci_qemu_pools"] for block in terraform["locals"] if "ci_qemu_pools" in block)

    runner_pools = {pool["asg_name"]: int(pool["max_instances"]) for pool in fox["gitlab_runner_aws_pools"]}
    assert runner_pools == {asg["name"]: asg["max_size"] for asg in asgs.values()}
