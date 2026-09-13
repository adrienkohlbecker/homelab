"""Unit tests for the promoted.json pointer shared by the qemu-image tasks.

The pointer object replaces the old SSM parameter as the live-build selector.
These cover the producer-side format (upload-s3.py ``pointer_body``) and the
consumer-side validation (hydrate-qemu-images.py ``resolve_image``), since
both must agree on the same JSON shape for S3.

The task scripts have hyphenated filenames, so they are loaded via
importlib.util.spec_from_file_location rather than a plain import. Loading is
side-effect-free: both modules do their work under ``if __name__ == "__main__"``.
"""

import argparse
import json
import re
from pathlib import Path

import pytest
from conftest import load_repo_module

upload = load_repo_module("mise-tasks/packer/upload-s3.py", name="upload_s3")
hydrate = load_repo_module("mise-tasks/ci/hydrate-qemu-images.py", name="hydrate_qemu_images")


def _args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "architecture": "x86_64",
        "bucket": "homelab-ci-images",
        "build_id": "ci-42-gdeadbeef0000",
        "machine": "box",
        "region": "eu-central-1",
        "source_sha": "d" * 40,
        "ubuntu": "noble",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class TestDefaultBuildId:
    def test_ci_retries_get_job_specific_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI_PIPELINE_ID", "42")
        monkeypatch.setenv("CI_JOB_ID", "1001")

        assert upload.default_build_id() == "42.1001"

        monkeypatch.setenv("CI_JOB_ID", "1002")
        assert upload.default_build_id() == "42.1002"

    def test_incomplete_ci_environment_uses_local_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(upload, "git_output", lambda _args: "deadbeef0000")
        monkeypatch.setenv("CI_PIPELINE_ID", "42")
        monkeypatch.delenv("CI_JOB_ID", raising=False)

        assert re.fullmatch(r"\d{8}T\d{6}Z-gdeadbeef0000", upload.default_build_id())


class TestPointerBody:
    def test_format_is_sorted_indented_trailing_newline(self) -> None:
        body = upload.pointer_body(_args(), ["ci-42-gdeadbeef0000", "previous"])
        assert body.endswith("\n")
        # sort_keys=True, indent=2
        assert body == (
            '{\n  "architecture": "x86_64",\n  "build_id": "ci-42-gdeadbeef0000",\n'
            '  "machine": "box",\n  "rollback_build_ids": [\n    "previous"\n  ],\n'
            f'  "source_sha": "{"d" * 40}",\n  "ubuntu": "noble"\n}}\n'
        )

    def test_pointer_name_constant_matches(self) -> None:
        assert upload.POINTER_NAME == "promoted.json"
        assert hydrate.POINTER_NAME == "promoted.json"

    def test_producer_and_consumer_support_all_fixture_machines(self) -> None:
        expected = {"box", "box_deps", "lab"}
        assert expected == upload.VALID_MACHINES
        assert expected == hydrate.VALID_MACHINES


class TestManifest:
    def test_round_trip_contains_only_identity_and_verified_files(
        self,
        tmp_path: Path,
    ) -> None:
        disk = tmp_path / "packer-ubuntu-1.raw"
        efivars = tmp_path / "efivars.fd"
        disk.write_bytes(b"disk")
        efivars.write_bytes(b"efi")

        args = _args()
        manifest = upload.build_manifest(args=args, disks=[disk], efivars=efivars)
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        assert set(manifest) == {
            "architecture",
            "build_id",
            "bundle_name",
            "files",
            "machine",
            "source_sha",
            "ubuntu",
        }
        assert manifest["files"] == [
            {"name": disk.name, "sha256": upload.sha256(disk)},
            {"name": efivars.name, "sha256": upload.sha256(efivars)},
        ]
        selection = hydrate.ImageSelection(args.build_id, args.source_sha)
        assert hydrate.read_manifest(manifest_path, args, selection) == manifest

    def test_manifest_without_files_is_rejected(self, tmp_path: Path) -> None:
        manifest = {
            "machine": "box",
            "ubuntu": "noble",
            "build_id": "ci-42-gdeadbeef0000",
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        with pytest.raises(SystemExit, match="manifest files must be a non-empty list"):
            hydrate.read_manifest(
                manifest_path,
                _args(),
                hydrate.ImageSelection(manifest["build_id"], None),
            )

    def test_missing_hash_is_rejected(self, tmp_path: Path) -> None:
        manifest = {
            "machine": "box",
            "ubuntu": "noble",
            "build_id": "ci-42-gdeadbeef0000",
            "files": [{"name": "disk.raw"}],
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        with pytest.raises(SystemExit, match="sha256 is invalid"):
            hydrate.read_manifest(
                manifest_path,
                _args(),
                hydrate.ImageSelection(manifest["build_id"], None),
            )

    def test_wrong_architecture_is_rejected(self, tmp_path: Path) -> None:
        args = _args(architecture="aarch64", bucket="homelab-ci-arm-images-eu-west-1", region="eu-west-1")
        manifest = {
            "architecture": "x86_64",
            "build_id": args.build_id,
            "bundle_name": "disks.tar.zst",
            "files": [{"name": "disk.raw", "sha256": "0" * 64}],
            "machine": args.machine,
            "source_sha": args.source_sha,
            "ubuntu": args.ubuntu,
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        with pytest.raises(SystemExit, match="manifest architecture mismatch"):
            hydrate.read_manifest(
                manifest_path,
                args,
                hydrate.ImageSelection(args.build_id, args.source_sha),
            )

    def test_pointer_and_manifest_source_sha_must_match(self, tmp_path: Path) -> None:
        args = _args()
        manifest = {
            "architecture": args.architecture,
            "build_id": args.build_id,
            "bundle_name": "disks.tar.zst",
            "files": [{"name": "disk.raw", "sha256": "0" * 64}],
            "machine": args.machine,
            "source_sha": "e" * 40,
            "ubuntu": args.ubuntu,
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        with pytest.raises(SystemExit, match="manifest source_sha mismatch"):
            hydrate.read_manifest(
                manifest_path,
                args,
                hydrate.ImageSelection(args.build_id, args.source_sha),
            )

    def test_legacy_x86_manifest_remains_readable(self, tmp_path: Path) -> None:
        args = _args()
        manifest = {
            "build_id": args.build_id,
            "bundle_name": "disks.tar.zst",
            "files": [{"name": "disk.raw", "sha256": "0" * 64}],
            "machine": args.machine,
            "ubuntu": args.ubuntu,
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        assert (
            hydrate.read_manifest(
                manifest_path,
                args,
                hydrate.ImageSelection(args.build_id, None),
            )
            == manifest
        )

    def test_extracted_hash_mismatch_is_rejected(self, tmp_path: Path) -> None:
        disk = tmp_path / "disk.raw"
        disk.write_bytes(b"corrupt")

        with pytest.raises(SystemExit, match="sha256 mismatch"):
            hydrate.verify_files(tmp_path, [{"name": disk.name, "sha256": "0" * 64}])


class TestRetention:
    def test_lists_build_objects_and_ignores_pointer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            upload,
            "output",
            lambda _argv: json.dumps(
                {
                    "Contents": [
                        {"Key": "noble/box/promoted.json", "LastModified": "2026-01-03T00:00:00Z"},
                        {"Key": "noble/box/b1/manifest.json", "LastModified": "2026-01-01T00:00:00Z"},
                        {"Key": "noble/box/b1/disks.tar.zst", "LastModified": "2026-01-02T00:00:00Z"},
                    ]
                }
            ),
        )

        assert upload.list_build_objects("bucket", "box", "noble", "region") == {
            "b1": {
                "last_modified": "2026-01-02T00:00:00Z",
                "keys": ["noble/box/b1/manifest.json", "noble/box/b1/disks.tar.zst"],
            }
        }

    def test_selects_promoted_and_three_newest_rollbacks(self) -> None:
        builds = {f"b{index}": {"last_modified": f"2026-01-0{index}T00:00:00Z", "keys": []} for index in range(1, 7)}

        assert upload.select_retained_builds(builds, "b4") == ["b4", "b6", "b5", "b3"]

    def test_tags_every_object_in_selected_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = []
        monkeypatch.setattr(upload, "tag_object", lambda *args: calls.append(args))
        builds = {
            "b2": {"last_modified": "2026-01-02T00:00:00Z", "keys": ["b2/manifest", "b2/bundle"]},
            "b1": {"last_modified": "2026-01-01T00:00:00Z", "keys": ["b1/manifest", "b1/bundle"]},
        }

        upload.tag_builds("bucket", builds, ["b2"], "region", state=upload.RETAINED_STATE)

        assert calls == [
            ("bucket", "b2/manifest", "retained", "region"),
            ("bucket", "b2/bundle", "retained", "region"),
        ]


class TestResolveImage:
    def _resolve(self, monkeypatch: pytest.MonkeyPatch, body: str, **arg_overrides: object):
        monkeypatch.setattr(hydrate, "output", lambda argv, **kw: body)
        base: dict[str, object] = {
            "architecture": "x86_64",
            "bucket": "homelab-ci-images",
            "build_id": None,
            "machine": "box",
            "region": "eu-central-1",
            "ubuntu": "noble",
        }
        base.update(arg_overrides)
        args = argparse.Namespace(**base)
        return hydrate.resolve_image(args)

    def test_reads_build_id_from_pointer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = upload.pointer_body(_args(build_id="ci-7-gabc", machine="box", ubuntu="noble"), ["ci-7-gabc"])
        assert self._resolve(monkeypatch, body) == hydrate.ImageSelection("ci-7-gabc", "d" * 40)

    def test_reads_arm_pointer_from_selected_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        args = _args(
            architecture="aarch64",
            bucket="homelab-ci-arm-images-eu-west-1",
            build_id="arm-build",
            region="eu-west-1",
        )
        body = upload.pointer_body(args, [args.build_id])

        def output(argv: list[str], **_kwargs: object) -> str:
            calls.append(argv)
            return body

        monkeypatch.setattr(hydrate, "output", output)
        selection_args = argparse.Namespace(
            architecture=args.architecture,
            bucket=args.bucket,
            build_id=None,
            machine=args.machine,
            region=args.region,
            ubuntu=args.ubuntu,
        )
        assert hydrate.resolve_image(selection_args) == hydrate.ImageSelection(args.build_id, args.source_sha)
        assert calls == [
            [
                "aws",
                "--region",
                args.region,
                "--cli-connect-timeout",
                "10",
                "--cli-read-timeout",
                "300",
                "s3",
                "cp",
                f"s3://{args.bucket}/noble/box/promoted.json",
                "-",
            ]
        ]

    def test_explicit_build_id_skips_pointer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hydrate, "output", lambda *_args, **_kwargs: pytest.fail("pointer read was attempted"))

        assert self._resolve(monkeypatch, "", build_id="selected") == hydrate.ImageSelection("selected", None)

    def test_legacy_x86_pointer_remains_readable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = json.dumps({"build_id": "legacy", "machine": "box", "ubuntu": "noble"})

        assert self._resolve(monkeypatch, body) == hydrate.ImageSelection("legacy", None)

    def test_arm_pointer_requires_architecture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = json.dumps({"build_id": "legacy", "machine": "box", "ubuntu": "noble"})

        with pytest.raises(SystemExit, match="architecture mismatch"):
            self._resolve(
                monkeypatch,
                body,
                architecture="aarch64",
                bucket="homelab-ci-arm-images-eu-west-1",
                region="eu-west-1",
            )

    @pytest.mark.parametrize(("field", "value"), [("machine", "box_deps"), ("ubuntu", "resolute")])
    def test_mismatch_raises(self, monkeypatch: pytest.MonkeyPatch, field: str, value: str) -> None:
        body = upload.pointer_body(_args(**{field: value}), ["ci-42-gdeadbeef0000"])
        with pytest.raises(SystemExit, match=f"{field} mismatch"):
            self._resolve(monkeypatch, body)

    def test_empty_pointer_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(SystemExit, match="missing or empty"):
            self._resolve(monkeypatch, "")
