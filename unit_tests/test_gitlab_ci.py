from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_child_pipeline_forwards_pipeline_variables() -> None:
    pipeline = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())

    assert pipeline["test_cells"]["trigger"]["forward"]["pipeline_variables"] is True
