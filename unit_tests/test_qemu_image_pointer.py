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
import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

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


class TestImageStore:
    @pytest.mark.parametrize(("machine", "architecture"), [("x86_64", "x86_64"), ("arm64", "aarch64")])
    def test_host_architecture_normalizes_platform_names(
        self, monkeypatch: pytest.MonkeyPatch, machine: str, architecture: str
    ) -> None:
        monkeypatch.setattr("platform.machine", lambda: machine)

        assert upload.host_architecture() == architecture

    def test_each_architecture_resolves_its_regional_store(self) -> None:
        assert upload.image_store("x86_64") == ("homelab-ci-images", "eu-central-1")
        assert upload.image_store("aarch64") == ("homelab-ci-arm-images-eu-central-1", "eu-central-1")
        assert {"aarch64", "x86_64"} == upload.VALID_ARCHITECTURES

    def test_unknown_architecture_is_rejected(self) -> None:
        with pytest.raises(KeyError):
            upload.image_store("sparc")


class TestValidateTarget:
    def test_matching_architecture_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(upload, "host_architecture", lambda: "aarch64")

        upload.validate_target(_args(architecture="aarch64"))

    def test_label_must_match_the_build_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(upload, "host_architecture", lambda: "aarch64")

        with pytest.raises(SystemExit, match="built on this aarch64 host as x86_64"):
            upload.validate_target(_args(architecture="x86_64"))


class TestSourceSha:
    def test_ci_commit_is_recorded_without_inspecting_the_checkout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI_COMMIT_SHA", "d" * 40)
        monkeypatch.setattr(upload, "git_output", lambda *_args, **_kwargs: pytest.fail("git consulted in CI"))

        assert upload.source_sha() == "d" * 40

    def test_clean_local_checkout_records_head(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CI_COMMIT_SHA", raising=False)
        replies = {"status": "", "rev-parse": "e" * 40}
        monkeypatch.setattr(upload, "git_output", lambda args, default="": replies[args[0]])

        assert upload.source_sha() == "e" * 40

    def test_modified_local_checkout_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CI_COMMIT_SHA", raising=False)
        replies = {"status": " M mise-tasks/packer/upload-s3.py", "rev-parse": "e" * 40}
        monkeypatch.setattr(upload, "git_output", lambda args, default="": replies[args[0]])

        with pytest.raises(SystemExit, match="tracked files have uncommitted changes"):
            upload.source_sha()


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


class TestManifest:
    def test_round_trip_contains_archive_hash_and_member_sizes(
        self,
        tmp_path: Path,
    ) -> None:
        disk = tmp_path / "packer-ubuntu-1.raw"
        efivars = tmp_path / "efivars.fd"
        disk.write_bytes(b"disk")
        efivars.write_bytes(b"efi")

        args = _args()
        manifest = upload.build_manifest(
            args=args,
            disks=[disk],
            efivars=efivars,
            bundle_sha256="a" * 64,
        )
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        assert set(manifest) == {
            "architecture",
            "build_id",
            "bundle",
            "files",
            "format_version",
            "machine",
            "source_sha",
            "ubuntu",
        }
        assert manifest["bundle"] == {"name": "disks.tar.zst", "sha256": "a" * 64}
        assert manifest["files"] == [
            {"name": disk.name, "size": 4},
            {"name": efivars.name, "size": 3},
        ]
        selection = hydrate.ImageSelection(args.build_id, args.source_sha)
        assert hydrate.read_manifest(manifest_path, args, selection) == manifest

    def test_manifest_without_files_is_rejected(self, tmp_path: Path) -> None:
        manifest = {
            "architecture": "x86_64",
            "bundle": {"name": "disks.tar.zst", "sha256": "a" * 64},
            "machine": "box",
            "format_version": 2,
            "source_sha": "d" * 40,
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

    def test_legacy_manifest_is_rejected(self, tmp_path: Path) -> None:
        manifest = {
            "architecture": "x86_64",
            "bundle": {"name": "disks.tar.zst", "sha256": "a" * 64},
            "machine": "box",
            "source_sha": "d" * 40,
            "ubuntu": "noble",
            "build_id": "ci-42-gdeadbeef0000",
            "files": [{"name": "disk.raw", "size": 4}],
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        with pytest.raises(SystemExit, match="unsupported manifest format_version"):
            hydrate.read_manifest(manifest_path, _args(), hydrate.ImageSelection(manifest["build_id"], None))

    @pytest.mark.parametrize("version", [1, 2.0, True, 3])
    def test_unsupported_manifest_versions_are_rejected(self, version: object) -> None:
        with pytest.raises(ValueError, match="unsupported manifest format_version"):
            hydrate.manifest_files({"format_version": version, "files": [{"name": "disk.raw", "size": 4}]})

    def test_manifest_requires_an_archive_hash(self, tmp_path: Path) -> None:
        args = _args()
        manifest = {
            "architecture": args.architecture,
            "build_id": args.build_id,
            "bundle": {"name": "disks.tar.zst"},
            "files": [{"name": "disk.raw", "size": 4}],
            "format_version": 2,
            "machine": args.machine,
            "source_sha": args.source_sha,
            "ubuntu": args.ubuntu,
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        with pytest.raises(SystemExit, match="manifest bundle sha256 is invalid"):
            hydrate.read_manifest(manifest_path, args, hydrate.ImageSelection(args.build_id, args.source_sha))

    def test_manifest_requires_member_sizes(self, tmp_path: Path) -> None:
        args = _args()
        manifest = {
            "architecture": args.architecture,
            "build_id": args.build_id,
            "bundle": {"name": "disks.tar.zst", "sha256": "a" * 64},
            "files": [{"name": "disk.raw"}],
            "format_version": 2,
            "machine": args.machine,
            "source_sha": args.source_sha,
            "ubuntu": args.ubuntu,
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        with pytest.raises(SystemExit, match="manifest file size is invalid"):
            hydrate.read_manifest(manifest_path, args, hydrate.ImageSelection(args.build_id, args.source_sha))

    def test_wrong_architecture_is_rejected(self, tmp_path: Path) -> None:
        args = _args(architecture="aarch64", bucket="homelab-ci-arm-images-eu-central-1", region="eu-central-1")
        manifest = {
            "architecture": "x86_64",
            "build_id": args.build_id,
            "bundle": {"name": "disks.tar.zst", "sha256": "a" * 64},
            "files": [{"name": "disk.raw", "size": 4}],
            "format_version": 2,
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
            "bundle": {"name": "disks.tar.zst", "sha256": "a" * 64},
            "files": [{"name": "disk.raw", "size": 4}],
            "format_version": 2,
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

    def test_unlabelled_x86_manifest_is_rejected(self, tmp_path: Path) -> None:
        args = _args()
        manifest = {
            "build_id": args.build_id,
            "bundle": {"name": "disks.tar.zst", "sha256": "a" * 64},
            "files": [{"name": "disk.raw", "size": 4}],
            "format_version": 2,
            "machine": args.machine,
            "ubuntu": args.ubuntu,
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        with pytest.raises(SystemExit, match="manifest architecture mismatch"):
            hydrate.read_manifest(manifest_path, args, hydrate.ImageSelection(args.build_id, None))

    def test_archive_hash_mismatch_is_rejected(self) -> None:
        archive = hydrate.DigestReader(io.BytesIO(b"archive"))
        archive.drain()

        with pytest.raises(SystemExit, match="bundle sha256 mismatch"):
            hydrate.verify_archive(archive, "0" * 64)


def _zstd_bundle(path: Path, members: list[tarfile.TarInfo | tuple[str, bytes]]) -> Path:
    with tarfile.open(path, "w:zst") as tar:
        for member in members:
            if isinstance(member, tarfile.TarInfo):
                tar.addfile(member)
                continue
            name, content = member
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return path


def _manifest_files(*members: tuple[str, int]) -> list[Any]:
    return [hydrate.ManifestFile(name, size) for name, size in members]


def _gnu_tar_with_zstd() -> str | None:
    tar = shutil.which("gtar") or shutil.which("tar")
    if tar is None or shutil.which("zstd") is None:
        return None
    version = subprocess.run([tar, "--version"], capture_output=True, text=True).stdout
    return tar if "GNU tar" in version else None


class TestExtractBundle:
    def test_hashes_the_complete_compressed_archive(self, tmp_path: Path) -> None:
        bundle = _zstd_bundle(tmp_path / "disks.tar.zst", [("disk.raw", b"disk")])
        staged = tmp_path / "image"
        staged.mkdir()

        with bundle.open("rb") as stream:
            archive = hydrate.DigestReader(stream)
            hydrate.extract_bundle(archive, staged, _manifest_files(("disk.raw", 4)))
            archive.drain()

        assert archive.hexdigest() == hashlib.sha256(bundle.read_bytes()).hexdigest()

    def test_extracts_exactly_the_manifest_members(self, tmp_path: Path) -> None:
        bundle = _zstd_bundle(tmp_path / "disks.tar.zst", [("disk.raw", b"disk"), ("efivars.fd", b"efi")])
        staged = tmp_path / "image"
        staged.mkdir()

        with bundle.open("rb") as stream:
            hydrate.extract_bundle(
                hydrate.DigestReader(stream),
                staged,
                _manifest_files(("disk.raw", 4), ("efivars.fd", 3)),
            )

        assert (staged / "disk.raw").read_bytes() == b"disk"
        assert (staged / "efivars.fd").read_bytes() == b"efi"

    def test_links_are_refused(self, tmp_path: Path) -> None:
        link = tarfile.TarInfo("disk.raw")
        link.type = tarfile.SYMTYPE
        link.linkname = "efivars.fd"
        bundle = _zstd_bundle(tmp_path / "disks.tar.zst", [("efivars.fd", b"efi"), link])
        staged = tmp_path / "image"
        staged.mkdir()

        with bundle.open("rb") as stream, pytest.raises(SystemExit, match="not a regular file"):
            hydrate.extract_bundle(
                hydrate.DigestReader(stream),
                staged,
                _manifest_files(("disk.raw", 0), ("efivars.fd", 3)),
            )
        assert not (staged / "disk.raw").exists()

    def test_traversal_is_refused_before_writing(self, tmp_path: Path) -> None:
        bundle = _zstd_bundle(tmp_path / "disks.tar.zst", [("../escaped", b"x")])
        staged = tmp_path / "image"
        staged.mkdir()

        with bundle.open("rb") as stream, pytest.raises(SystemExit, match="unsafe archive member"):
            hydrate.extract_bundle(hydrate.DigestReader(stream), staged, _manifest_files(("../escaped", 1)))
        assert not (tmp_path / "escaped").exists()

    def test_unlisted_members_are_refused(self, tmp_path: Path) -> None:
        bundle = _zstd_bundle(tmp_path / "disks.tar.zst", [("disk.raw", b"disk"), ("extra", b"x")])
        staged = tmp_path / "image"
        staged.mkdir()

        with bundle.open("rb") as stream, pytest.raises(SystemExit, match="not in the manifest: 'extra'"):
            hydrate.extract_bundle(hydrate.DigestReader(stream), staged, _manifest_files(("disk.raw", 4)))
        assert not (staged / "extra").exists()

    def test_missing_members_are_reported(self, tmp_path: Path) -> None:
        bundle = _zstd_bundle(tmp_path / "disks.tar.zst", [("disk.raw", b"disk")])
        staged = tmp_path / "image"
        staged.mkdir()

        with bundle.open("rb") as stream, pytest.raises(SystemExit, match=r"missing manifest members: efivars\.fd"):
            hydrate.extract_bundle(
                hydrate.DigestReader(stream),
                staged,
                _manifest_files(("disk.raw", 4), ("efivars.fd", 3)),
            )

    def test_duplicate_members_are_refused(self, tmp_path: Path) -> None:
        bundle = _zstd_bundle(tmp_path / "disks.tar.zst", [("disk.raw", b"disk"), ("disk.raw", b"dusk")])
        staged = tmp_path / "image"
        staged.mkdir()

        with bundle.open("rb") as stream, pytest.raises(SystemExit, match="archive member is duplicated"):
            hydrate.extract_bundle(hydrate.DigestReader(stream), staged, _manifest_files(("disk.raw", 4)))

    def test_member_size_must_match_the_manifest(self, tmp_path: Path) -> None:
        bundle = _zstd_bundle(tmp_path / "disks.tar.zst", [("disk.raw", b"disk")])
        staged = tmp_path / "image"
        staged.mkdir()

        with bundle.open("rb") as stream, pytest.raises(SystemExit, match="size mismatch"):
            hydrate.extract_bundle(hydrate.DigestReader(stream), staged, _manifest_files(("disk.raw", 5)))

    @pytest.mark.skipif(_gnu_tar_with_zstd() is None, reason="needs GNU tar with zstd, as upload-s3 uses")
    def test_restores_gnu_sparse_bundles_from_upload(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir()
        disk = source / "packer-ubuntu-1.raw"
        with disk.open("wb") as handle:
            handle.truncate(64 * 1024 * 1024)
            handle.seek(48 * 1024 * 1024)
            handle.write(b"data after a hole")
        (source / "efivars.fd").write_bytes(b"efi")
        bundle = tmp_path / "disks.tar.zst"
        tar = _gnu_tar_with_zstd()
        assert tar is not None
        subprocess.run(
            [tar, "--sparse", "--zstd", "-cf", str(bundle), "-C", str(source), disk.name, "efivars.fd"], check=True
        )
        staged = tmp_path / "image"
        staged.mkdir()

        with bundle.open("rb") as stream:
            hydrate.extract_bundle(
                hydrate.DigestReader(stream),
                staged,
                _manifest_files((disk.name, disk.stat().st_size), ("efivars.fd", 3)),
            )

        restored = (staged / disk.name).stat()
        with (staged / disk.name).open("rb") as restored_file, disk.open("rb") as source_file:
            assert (
                hashlib.file_digest(restored_file, "sha256").digest()
                == hashlib.file_digest(source_file, "sha256").digest()
            )
        # Filesystems round data extents differently (APFS reports 16 MiB), so
        # only require that the 48 MiB hole was not written out.
        assert restored.st_blocks * 512 < restored.st_size // 2


class TestDownloadStream:
    class FakeProcess:
        def __init__(self, argv: list[str], *, stdout: int) -> None:
            assert stdout == subprocess.PIPE
            self.args = argv
            self.stdout = io.BytesIO(b"bundle")
            self.returncode = 0
            self.terminated = False

        def wait(self) -> int:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True

    def test_streams_s3_object_to_stdout_with_checksum_validation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        process: TestDownloadStream.FakeProcess | None = None

        def popen(argv: list[str], *, stdout: int) -> TestDownloadStream.FakeProcess:
            nonlocal process
            process = self.FakeProcess(argv, stdout=stdout)
            return process

        monkeypatch.setattr(hydrate.subprocess, "Popen", popen)

        with hydrate.download_s3_stream(_args(), "noble/box/build/disks.tar.zst") as stream:
            assert stream.read() == b"bundle"

        assert process is not None
        assert process.args[-4:] == ["-", "--only-show-errors", "--checksum-mode", "ENABLED"]
        assert "s3://homelab-ci-images/noble/box/build/disks.tar.zst" in process.args

    def test_failed_download_is_not_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        process = self.FakeProcess([], stdout=subprocess.PIPE)
        process.returncode = 1
        monkeypatch.setattr(hydrate.subprocess, "Popen", lambda *_args, **_kwargs: process)

        with pytest.raises(subprocess.CalledProcessError), hydrate.download_s3_stream(_args(), "bundle") as stream:
            stream.read()

    def test_consumer_failure_terminates_download(self, monkeypatch: pytest.MonkeyPatch) -> None:
        process = self.FakeProcess([], stdout=subprocess.PIPE)
        monkeypatch.setattr(hydrate.subprocess, "Popen", lambda *_args, **_kwargs: process)

        with pytest.raises(RuntimeError, match="stop"), hydrate.download_s3_stream(_args(), "bundle"):
            raise RuntimeError("stop")

        assert process.terminated


class TestLocalCache:
    def _hydrated_tree(self, tmp_path: Path) -> tuple[Path, argparse.Namespace, object]:
        target = tmp_path / "noble" / "box"
        target.mkdir(parents=True)
        disk = target / "packer-ubuntu-1.raw"
        efivars = target / "efivars.fd"
        disk.write_bytes(b"disk")
        efivars.write_bytes(b"efi")
        args = _args()
        manifest = upload.build_manifest(
            args=args,
            disks=[disk],
            efivars=efivars,
            bundle_sha256="a" * 64,
        )
        manifest[hydrate.LOCAL_FILES_KEY] = hydrate.cache_file_fingerprints(
            target,
            hydrate.manifest_files(manifest),
        )
        (target / hydrate.LOCAL_MANIFEST_NAME).write_text(json.dumps(manifest))
        return target, args, hydrate.ImageSelection(args.build_id, args.source_sha)

    def test_untouched_cache_is_reused(self, tmp_path: Path) -> None:
        target, args, selection = self._hydrated_tree(tmp_path)

        assert hydrate.local_cache_complete(target, args, selection)

    def test_cache_hit_does_not_reread_members(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target, args, selection = self._hydrated_tree(tmp_path)
        original_open = Path.open

        def forbid_member_open(path: Path, *open_args: Any, **open_kwargs: Any) -> Any:
            if path.name in {"packer-ubuntu-1.raw", "efivars.fd"}:
                pytest.fail(f"cache member reread: {path}")
            return original_open(path, *open_args, **open_kwargs)

        monkeypatch.setattr(Path, "open", forbid_member_open)

        assert hydrate.local_cache_complete(target, args, selection)

    def test_modified_member_is_rehydrated(self, tmp_path: Path) -> None:
        target, args, selection = self._hydrated_tree(tmp_path)
        (target / "packer-ubuntu-1.raw").write_bytes(b"dusk")

        assert not hydrate.local_cache_complete(target, args, selection)

    def test_added_member_is_rehydrated(self, tmp_path: Path) -> None:
        target, args, selection = self._hydrated_tree(tmp_path)
        (target / "packer-ubuntu-3.raw").write_bytes(b"extra")

        assert not hydrate.local_cache_complete(target, args, selection)

    def test_legacy_marker_is_rehydrated(self, tmp_path: Path) -> None:
        target, args, selection = self._hydrated_tree(tmp_path)
        (target / ".homelab_s3_build_id").write_text(f"{args.build_id}\n")

        assert not hydrate.local_cache_complete(target, args, selection)

    def test_cache_without_fingerprints_is_rehydrated(self, tmp_path: Path) -> None:
        target, args, selection = self._hydrated_tree(tmp_path)
        manifest_path = target / hydrate.LOCAL_MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text())
        del manifest[hydrate.LOCAL_FILES_KEY]
        (target / hydrate.LOCAL_MANIFEST_NAME).write_text(json.dumps(manifest))

        assert not hydrate.local_cache_complete(target, args, selection)


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


class TestConditionalWrites:
    @staticmethod
    def _fake_aws(
        monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0, stderr: str = "", stdout: str = ""
    ) -> list[list[str]]:
        calls: list[list[str]] = []

        def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append([*argv, Path(argv[argv.index("--body") + 1]).read_text()] if "--body" in argv else argv)
            if "get-object" in argv and returncode == 0:
                Path(argv[-1]).write_text('{"build_id": "old"}\n')
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

        monkeypatch.setattr(upload.subprocess, "run", run)
        return calls

    def test_pointer_replacement_must_match_the_etag_it_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._fake_aws(monkeypatch)
        current = upload.CurrentPointer(body="{}", etag='"abc"')

        upload.write_pointer("bucket", "noble/box/promoted.json", "new body\n", "region", current)

        assert calls[0][calls[0].index("--if-match") + 1] == '"abc"'
        assert "--if-none-match" not in calls[0]
        assert calls[0][-1] == "new body\n"

    def test_first_pointer_requires_the_key_to_be_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._fake_aws(monkeypatch)

        upload.write_pointer("bucket", "noble/box/promoted.json", "body\n", "region", None)

        assert calls[0][calls[0].index("--if-none-match") + 1] == "*"

    @pytest.mark.parametrize("error", ["PreconditionFailed", "ConditionalRequestConflict"])
    def test_concurrent_promotion_exits_cleanly(self, monkeypatch: pytest.MonkeyPatch, error: str) -> None:
        self._fake_aws(monkeypatch, returncode=254, stderr=f"An error occurred ({error}) when calling PutObject")

        with pytest.raises(SystemExit, match="changed during promotion"):
            upload.write_pointer("bucket", "noble/box/promoted.json", "body\n", "region", None)

    def test_existing_manifest_is_never_overwritten(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        calls = self._fake_aws(monkeypatch, returncode=254, stderr="An error occurred (PreconditionFailed)")
        manifest = tmp_path / "manifest.json"
        manifest.write_text("{}\n")

        with pytest.raises(SystemExit, match="refusing to overwrite existing object"):
            upload.publish_manifest("bucket", manifest, "noble/box/b1/manifest.json", "region")
        assert calls[0][calls[0].index("--if-none-match") + 1] == "*"

    def test_other_write_failures_propagate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._fake_aws(monkeypatch, returncode=254, stderr="An error occurred (AccessDenied)")

        with pytest.raises(subprocess.CalledProcessError):
            upload.write_pointer("bucket", "noble/box/promoted.json", "body\n", "region", None)

    def test_reads_pointer_body_with_its_etag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._fake_aws(monkeypatch, stdout='{"ETag": "\\"abc\\""}')

        assert upload.read_pointer("bucket", "noble/box/promoted.json", "region") == upload.CurrentPointer(
            body='{"build_id": "old"}\n', etag='"abc"'
        )

    def test_missing_pointer_reads_as_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._fake_aws(monkeypatch, returncode=254, stderr="An error occurred (NoSuchKey)")

        assert upload.read_pointer("bucket", "noble/box/promoted.json", "region") is None

    def test_unreadable_pointer_is_not_treated_as_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._fake_aws(monkeypatch, returncode=254, stderr="An error occurred (AccessDenied)")

        with pytest.raises(subprocess.CalledProcessError):
            upload.read_pointer("bucket", "noble/box/promoted.json", "region")


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
            bucket="homelab-ci-arm-images-eu-central-1",
            build_id="arm-build",
            region="eu-central-1",
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

    @pytest.mark.parametrize("architecture", ["x86_64", "aarch64"])
    def test_unlabelled_pointer_is_rejected(self, monkeypatch: pytest.MonkeyPatch, architecture: str) -> None:
        body = json.dumps({"build_id": "legacy", "machine": "box", "ubuntu": "noble"})

        with pytest.raises(SystemExit, match="architecture mismatch"):
            self._resolve(monkeypatch, body, architecture=architecture)

    @pytest.mark.parametrize(("field", "value"), [("machine", "box_deps"), ("ubuntu", "resolute")])
    def test_mismatch_raises(self, monkeypatch: pytest.MonkeyPatch, field: str, value: str) -> None:
        body = upload.pointer_body(_args(**{field: value}), ["ci-42-gdeadbeef0000"])
        with pytest.raises(SystemExit, match=f"{field} mismatch"):
            self._resolve(monkeypatch, body)

    def test_empty_pointer_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(SystemExit, match="missing or empty"):
            self._resolve(monkeypatch, "")
