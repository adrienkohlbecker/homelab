from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
PIPELINE = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())


def test_ansible_config_is_global() -> None:
    assert PIPELINE["variables"]["ANSIBLE_CONFIG"] == "$CI_PROJECT_DIR/ansible.cfg"


def test_child_pipeline_forwards_pipeline_variables() -> None:
    assert PIPELINE["test_cells"]["trigger"]["forward"]["pipeline_variables"] is True


def test_lab_qemu_image_is_published_for_supported_releases() -> None:
    assert PIPELINE[".qemu_image"]["parallel"]["matrix"] == [{"UBUNTU": ["noble", "resolute"]}]
    assert PIPELINE["qemu_image:lab"] == {
        "extends": ".qemu_image",
        "resource_group": "qemu_image_lab_$UBUNTU",
        "script": ['mise run packer:publish-qemu lab --ubuntu "$UBUNTU" --promote'],
    }
