from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_ansible_config_is_global() -> None:
    pipeline = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())

    assert pipeline["variables"]["ANSIBLE_CONFIG"] == "$CI_PROJECT_DIR/ansible.cfg"


def test_child_pipeline_forwards_pipeline_variables() -> None:
    pipeline = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())

    assert pipeline["test_cells"]["trigger"]["forward"]["pipeline_variables"] is True


def test_lab_qemu_image_is_published_for_supported_releases() -> None:
    pipeline = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())

    assert pipeline[".qemu_image"]["parallel"]["matrix"] == [{"UBUNTU": ["noble", "resolute"]}]
    assert pipeline["qemu_image:lab"] == {
        "extends": ".qemu_image",
        "resource_group": "qemu_image_lab_$UBUNTU",
        "script": ['mise run packer:publish-qemu lab --ubuntu "$UBUNTU" --promote'],
    }


def test_qemu_host_ami_uses_one_architecture_matrix_and_promotion_flow() -> None:
    pipeline = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
    job = pipeline["qemu_host_ami"]

    assert job["parallel"]["matrix"] == [
        {
            "QEMU_HOST_ARCHITECTURE": "x86_64",
            "AWS_REGION": "eu-central-1",
            "MISE_DISABLE_TOOLS": "",
        },
        {
            "QEMU_HOST_ARCHITECTURE": "aarch64",
            "AWS_REGION": "eu-west-1",
            "MISE_DISABLE_TOOLS": "aqua:Kampfkarren/selene",
        },
    ]
    assert job["resource_group"] == "ami-qemu-host-$QEMU_HOST_ARCHITECTURE-noble"
    assert job["script"][-2:] == [
        'source mise-tasks/ci/aws-oidc.sh arn:aws:iam::000390721279:role/homelab-ci-bake "qemu-host-ami-$CI_JOB_ID" --region "$AWS_REGION"',
        'mise run packer:qemu-host-ami --architecture "$QEMU_HOST_ARCHITECTURE" --region "$AWS_REGION" --promote',
    ]
