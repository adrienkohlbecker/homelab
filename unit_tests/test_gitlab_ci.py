from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_ansible_config_is_global() -> None:
    pipeline = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())

    assert pipeline["variables"]["ANSIBLE_CONFIG"] == "$CI_PROJECT_DIR/ansible.cfg"
    assert pipeline["variables"]["HOMELAB_CI_ARM"] == "auto"
    assert pipeline["variables"]["HOMELAB_CI_BENCHMARK_ONLY"] == "false"


def test_benchmark_only_mode_skips_regular_cell_pipeline() -> None:
    pipeline = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
    skip_rule = {"if": '$HOMELAB_CI_BENCHMARK_ONLY == "true"', "when": "never"}

    assert pipeline["detect"]["rules"][0] == skip_rule
    assert pipeline["test_cells"]["rules"][0] == skip_rule


def test_child_pipeline_forwards_pipeline_variables() -> None:
    pipeline = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())

    assert pipeline["test_cells"]["trigger"]["forward"]["pipeline_variables"] is True


def test_arm_density_is_one_protected_manual_child_trigger() -> None:
    pipeline = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
    trigger = pipeline["arm_density"]

    assert trigger["extends"] == ".protected_manual_job"
    assert trigger["trigger"]["include"] == [{"local": "mise-tasks/ci/arm_density.yml"}]
    assert trigger["trigger"]["forward"]["pipeline_variables"] is True
    assert trigger["trigger"]["strategy"] == "depend"


def test_arm_benchmark_is_one_protected_manual_child_trigger() -> None:
    pipeline = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
    trigger = pipeline["arm_benchmark"]

    assert trigger["extends"] == ".protected_manual_job"
    assert trigger["needs"] == []
    assert trigger["trigger"]["include"] == [{"local": "mise-tasks/ci/arm_benchmark.yml"}]
    assert trigger["trigger"]["forward"]["pipeline_variables"] is True
    assert trigger["trigger"]["strategy"] == "depend"


def test_arm_benchmark_repeats_full_universe_on_existing_images() -> None:
    child = yaml.safe_load((ROOT / "mise-tasks" / "ci" / "arm_benchmark.yml").read_text())
    scaffold = child[".arm_benchmark_cell"]

    assert child["stages"] == ["benchmark"]
    assert scaffold["dependencies"] == []
    assert scaffold["tags"] == ["aws-shell-qemu-arm"]
    assert scaffold["variables"]["ARCH"] == "aarch64"
    before_script = "\n".join(scaffold["before_script"])
    assert "--arm-benchmark-index" in before_script
    assert "--bucket homelab-ci-arm-images-eu-west-1" in before_script
    assert "export UBUNTU=noble" in before_script
    assert "HOMELAB_TEST_OUT_DIR" in before_script

    jobs = [child[f"arm_benchmark:{repetition}"] for repetition in range(1, 4)]
    assert all(job["parallel"] == 130 for job in jobs)
    assert sum(job["parallel"] for job in jobs) == 390


def test_arm_density_child_has_sequential_unique_automatic_waves() -> None:
    child = yaml.safe_load((ROOT / "mise-tasks" / "ci" / "arm_density.yml").read_text())
    waves = (13, 26, 39, 52, 65, 78)
    scaffold = child[".arm_density_cell"]

    assert child["stages"] == [f"density_{size}" for size in waves]
    assert scaffold["dependencies"] == []
    assert scaffold["tags"] == ["aws-shell-qemu-arm"]
    assert scaffold["variables"]["ARCH"] == "aarch64"
    assert scaffold["variables"]["MISE_DISABLE_TOOLS"] == "aqua:Kampfkarren/selene"
    assert scaffold["variables"]["HOMELAB_AARCH64_FIRMWARE_DIR"] == ("/opt/homelab-ci/qemu-firmware/aarch64")
    before_script = "\n".join(scaffold["before_script"])
    assert "--arm-density-index" in before_script
    assert "--region eu-west-1" in before_script
    assert "--bucket homelab-ci-arm-images-eu-west-1" in before_script
    assert "HOMELAB_TEST_OUT_DIR" in before_script
    artifact_path = scaffold["artifacts"]["paths"][0]
    assert "$CI_JOB_NAME_SLUG" in artifact_path
    assert "$CI_NODE_INDEX" in artifact_path

    expanded_names: list[str] = []
    expanded_paths: list[str] = []
    for size in waves:
        name = f"arm_density:{size}"
        job = child[name]
        assert job["stage"] == f"density_{size}"
        assert job["parallel"] == size
        assert "when" not in job
        for index in range(1, size + 1):
            expanded_names.append(f"{name} {index}/{size}")
            expanded_paths.append(f"test/out/arm_density/arm-density-{size}-{index}/")

    assert len(expanded_names) == sum(waves)
    assert len(expanded_names) == len(set(expanded_names))
    assert len(expanded_paths) == len(set(expanded_paths))


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
