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


def test_arm_qemu_images_use_isolated_ireland_builder_jobs() -> None:
    pipeline = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
    scaffold = pipeline[".qemu_image_arm"]
    box = pipeline["qemu_image:box:arm"]
    box_deps = pipeline["qemu_image:box_deps:arm"]

    assert scaffold["tags"] == ["aws-shell-qemu-arm"]
    assert scaffold["variables"]["UBUNTU"] == "noble"
    assert scaffold["variables"]["MISE_DISABLE_TOOLS"] == "aqua:Kampfkarren/selene"
    for variable in (
        "HOME",
        "XDG_CONFIG_HOME",
        "PACKER_PLUGIN_PATH",
        "MISE_DATA_DIR",
        "UV_CACHE_DIR",
        "HOMELAB_CI_DIR",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
    ):
        assert "$CI_JOB_ID" in scaffold["variables"][variable]
    assert "--region eu-west-1" in scaffold["before_script"][-1]
    assert scaffold["after_script"] == ['rm -f "$CI_PROJECT_DIR/.aws_web_identity_token"']

    assert box["resource_group"] == "qemu_image_box_aarch64_$UBUNTU"
    assert "--bucket homelab-ci-arm-images-eu-west-1" in box["script"][0]
    assert "--region eu-west-1 --architecture aarch64" in box["script"][0]
    assert '--build-id "$CI_PIPELINE_ID.arm-box-$UBUNTU"' in box["script"][0]

    assert box_deps["resource_group"] == "qemu_image_box_deps_aarch64_$UBUNTU"
    assert box_deps["needs"] == [{"job": "qemu_image:box:arm", "artifacts": False}]
    assert '--base-build-id "$CI_PIPELINE_ID.arm-box-$UBUNTU"' in box_deps["script"][0]
    assert '--build-id "$CI_PIPELINE_ID.arm-box-deps-$UBUNTU"' in box_deps["script"][0]


def test_bake_role_covers_both_qemu_image_regions() -> None:
    terraform = (ROOT / "terraform" / "aws_ci.tf").read_text()

    assert '"aws:RequestedRegion" = [local.ci_aws_region, local.ci_arm_aws_region]' in terraform
    assert "aws_s3_bucket.ci_qemu_arm_images.arn" in terraform
    assert '"${aws_s3_bucket.ci_qemu_arm_images.arn}/*"' in terraform
