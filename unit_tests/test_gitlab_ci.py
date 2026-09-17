from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
PIPELINE = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())


def test_ansible_config_is_global() -> None:
    assert PIPELINE["variables"]["ANSIBLE_CONFIG"] == "$CI_PROJECT_DIR/ansible.cfg"
    assert "HOMELAB_CI_ARM" not in PIPELINE["variables"]
    assert PIPELINE["detect"]["script"][-1] == 'mise run ci:detect --target "$HOMELAB_CI_TARGET"'


def test_child_pipeline_forwards_pipeline_variables() -> None:
    assert PIPELINE["test_cells"]["trigger"]["forward"]["pipeline_variables"] is True


def test_api_pipelines_run_regular_jobs() -> None:
    source_rule = '$CI_PIPELINE_SOURCE == "web" || $CI_PIPELINE_SOURCE == "push" || $CI_PIPELINE_SOURCE == "api"'

    assert PIPELINE[".standard_pipeline"]["rules"] == [{"if": source_rule}]
    assert PIPELINE["detect"]["rules"][1]["if"] == source_rule
    assert PIPELINE["test_cells"]["rules"] == [{"if": source_rule}]
    for job in ("lint", "unit_tests"):
        assert ".standard_pipeline" in PIPELINE[job]["extends"]


def test_lab_qemu_image_is_published_for_supported_releases() -> None:
    assert PIPELINE[".qemu_image"]["parallel"]["matrix"] == [{"UBUNTU": ["noble", "resolute"]}]
    # lab's persistent shell runner must not keep the bake role's token.
    assert PIPELINE[".qemu_image"]["after_script"] == ['rm -f "$CI_PROJECT_DIR/.aws_web_identity_token"']
    assert PIPELINE["qemu_image:lab"] == {
        "extends": ".qemu_image",
        "resource_group": "qemu_image_lab_$UBUNTU",
        "script": ['mise run packer:publish-qemu lab --ubuntu "$UBUNTU" --promote'],
    }
    assert PIPELINE[".qemu_image"]["extends"] == ".protected_manual_job"
    assert PIPELINE["qemu_image:pug"] == {
        "extends": ".qemu_image",
        "resource_group": "qemu_image_pug_$UBUNTU",
        "script": ['mise run packer:publish-qemu pug --ubuntu "$UBUNTU" --promote'],
    }


def test_qemu_host_ami_uses_one_architecture_matrix_and_promotion_flow() -> None:
    pipeline = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
    job = pipeline["qemu_host_ami"]

    # The hosted amd64 job only drives Packer. The AMI's reduced ARM toolset
    # comes from qemu_host.pkr.hcl, not from this job's environment.
    assert job["parallel"]["matrix"] == [
        {"QEMU_HOST_ARCHITECTURE": "x86_64"},
        {"QEMU_HOST_ARCHITECTURE": "aarch64"},
    ]
    assert job["resource_group"] == "ami-qemu-host-$QEMU_HOST_ARCHITECTURE-noble"
    assert job["script"][-2:] == [
        'source mise-tasks/ci/aws-oidc.sh arn:aws:iam::000390721279:role/homelab-ci-bake "qemu-host-ami-$CI_JOB_ID" --region "$AWS_REGION"',
        'mise run packer:qemu-host-ami --architecture "$QEMU_HOST_ARCHITECTURE" --promote',
    ]


def test_arm_qemu_images_use_frankfurt_builder_jobs() -> None:
    pipeline = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
    scaffold = pipeline[".qemu_image_arm"]
    lab = pipeline["qemu_image:lab:arm"]

    assert scaffold["extends"] == ".protected_manual_job"
    assert pipeline[".protected_manual_job"]["rules"] == [
        {"if": '$CI_COMMIT_REF_PROTECTED == "true"', "when": "manual", "allow_failure": True}
    ]
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
    assert "--region" not in scaffold["before_script"][-1]
    assert scaffold["after_script"] == ['rm -f "$CI_PROJECT_DIR/.aws_web_identity_token"']

    assert lab == {
        "extends": ".qemu_image_arm",
        "resource_group": "qemu_image_lab_aarch64_$UBUNTU",
        "script": ['mise run packer:publish-qemu lab --ubuntu "$UBUNTU" --architecture aarch64 --promote'],
    }
    assert "qemu_image:pug:arm" not in pipeline


def test_bake_role_uses_shared_qemu_image_bucket() -> None:
    terraform = (ROOT / "terraform" / "aws_ci.tf").read_text()

    assert '"aws:RequestedRegion" = local.ci_aws_region' in terraform
    assert "aws_s3_bucket.ci_qemu_images.arn" in terraform
    assert '"${aws_s3_bucket.ci_qemu_images.arn}/*"' in terraform
    assert "ci_qemu_arm_images" not in terraform
