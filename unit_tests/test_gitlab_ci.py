from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_ansible_config_is_global() -> None:
    pipeline = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())

    assert pipeline["variables"]["ANSIBLE_CONFIG"] == "$CI_PROJECT_DIR/ansible.cfg"


def test_child_pipeline_forwards_pipeline_variables() -> None:
    pipeline = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())

    assert pipeline["test_cells"]["trigger"]["forward"]["pipeline_variables"] is True
