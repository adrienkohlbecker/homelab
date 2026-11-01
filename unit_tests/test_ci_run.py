import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).parents[1] / "mise-tasks" / "ci" / "run.py"
_SPEC = importlib.util.spec_from_file_location("ci_run", _MODULE_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
ci_run = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ci_run)


def fake_gitlab(stored_variables):
    """Fake glab_api: create pipeline 7, then report stored_variables for it."""
    calls = []

    def glab_api(path, method="GET", body=None):
        calls.append((method, path, body))
        if method == "POST":
            return {"id": 7, "web_url": "https://gitlab.example/pipelines/7"}
        return stored_variables

    return glab_api, calls


def test_unset_options_leave_gitlab_defaults() -> None:
    assert ci_run.pipeline_variables() == []


def test_options_map_to_pipeline_variables() -> None:
    assert ci_run.pipeline_variables(
        roles="SITE,nginx",
        target="lab",
        base_ref="HEAD~3",
        no_arm=True,
        host_ubuntu="resolute",
        next_ubuntu="stork",
        fox_ubuntu="stork",
    ) == [
        {"key": "ROLES", "value": "SITE,nginx"},
        {"key": "HOMELAB_CI_TARGET", "value": "lab"},
        {"key": "CI_BASE_REF", "value": "HEAD~3"},
        {"key": "HOMELAB_CI_ARM", "value": "false"},
        {"key": "CI_HOST_UBUNTU", "value": "resolute"},
        {"key": "NEXT_UBUNTU", "value": "stork"},
        {"key": "FOX_UBUNTU", "value": "stork"},
    ]


def test_every_variable_is_read_by_the_pipeline() -> None:
    # A variable nothing reads would be accepted by GitLab and silently ignored.
    ci_config = (Path(__file__).parents[1] / ".gitlab-ci.yml").read_text()
    detect = (Path(__file__).parents[1] / "mise-tasks" / "ci" / "detect.py").read_text()
    assert [name for name in ci_run.VARIABLES.values() if name not in ci_config + detect] == []


def test_unknown_option_is_rejected() -> None:
    with pytest.raises(TypeError, match="ubuntu"):
        ci_run.pipeline_variables(ubuntu="noble")


def test_trigger_posts_variables_as_json_body(monkeypatch) -> None:
    variables = [{"key": "ROLES", "value": "lnav"}]
    glab_api, calls = fake_gitlab([{"variable_type": "env_var", "key": "ROLES", "value": "lnav", "raw": False}])
    monkeypatch.setattr(ci_run, "glab_api", glab_api)

    assert ci_run.trigger("master", variables)["id"] == 7
    assert calls[0] == ("POST", "projects/:id/pipeline", {"ref": "master", "variables": variables})


def test_trigger_fails_when_gitlab_drops_variables(monkeypatch) -> None:
    glab_api, _ = fake_gitlab([])
    monkeypatch.setattr(ci_run, "glab_api", glab_api)

    with pytest.raises(ci_run.PipelineVariablesMismatch, match=r"pipelines/7 has variables \{\}"):
        ci_run.trigger("master", [{"key": "ROLES", "value": "lnav"}])
