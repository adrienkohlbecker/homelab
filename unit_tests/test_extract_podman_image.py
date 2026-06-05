"""Unit tests for roles/systemd_unit/library/extract_podman_image.py.

Tests the pure-logic functions (bus_label_escape, is_podman_run, find_image)
without needing systemd D-Bus or a running podman.
"""

import importlib
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "roles" / "systemd_unit" / "library" / "extract_podman_image.py"


def _load():
    spec = importlib.util.spec_from_file_location("extract_podman_image", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


epi = _load()


# ---------------------------------------------------------------------------
# bus_label_escape
# ---------------------------------------------------------------------------


class TestBusLabelEscape:
    def test_alphanumeric_passthrough(self) -> None:
        assert epi.bus_label_escape("hello123") == "hello123"

    def test_dot_escaped(self) -> None:
        assert epi.bus_label_escape("foo.service") == "foo_2eservice"

    def test_hyphen_escaped(self) -> None:
        assert epi.bus_label_escape("my-unit") == "my_2dunit"

    def test_at_escaped(self) -> None:
        assert epi.bus_label_escape("foo@bar.service") == "foo_40bar_2eservice"

    def test_empty_string(self) -> None:
        assert epi.bus_label_escape("") == ""

    def test_all_special(self) -> None:
        result = epi.bus_label_escape("-.@")
        assert result == "_2d_2e_40"


# ---------------------------------------------------------------------------
# is_podman_run
# ---------------------------------------------------------------------------


class TestIsPodmanRun:
    def test_basic(self) -> None:
        assert epi.is_podman_run(["podman", "run", "--rm", "alpine"])

    def test_full_path(self) -> None:
        assert epi.is_podman_run(["/usr/bin/podman", "run", "img"])

    def test_not_run(self) -> None:
        assert not epi.is_podman_run(["podman", "pull", "img"])

    def test_too_short(self) -> None:
        assert not epi.is_podman_run(["podman", "run"])

    def test_not_podman(self) -> None:
        assert not epi.is_podman_run(["docker", "run", "img"])

    def test_empty(self) -> None:
        assert not epi.is_podman_run([])


# ---------------------------------------------------------------------------
# find_image
# ---------------------------------------------------------------------------


class TestFindImage:
    FLAGS_NO_VALUE = {"--rm", "--init", "-d", "--detach", "-t", "--tty", "-i", "--interactive"}

    def test_simple(self) -> None:
        argv = ["podman", "run", "--rm", "alpine"]
        assert epi.find_image(argv, self.FLAGS_NO_VALUE) == "alpine"

    def test_with_cmd(self) -> None:
        argv = ["podman", "run", "--rm", "redis:7", "redis-server", "--save", "60", "1"]
        assert epi.find_image(argv, self.FLAGS_NO_VALUE) == "redis:7"

    def test_value_option_equals(self) -> None:
        argv = ["podman", "run", "--name=mycontainer", "alpine"]
        assert epi.find_image(argv, self.FLAGS_NO_VALUE) == "alpine"

    def test_value_option_space(self) -> None:
        argv = ["podman", "run", "--name", "mycontainer", "alpine"]
        assert epi.find_image(argv, self.FLAGS_NO_VALUE) == "alpine"

    def test_multiple_flags(self) -> None:
        argv = [
            "podman",
            "run",
            "--rm",
            "-d",
            "--name",
            "mycontainer",
            "--network",
            "mynet",
            "-v",
            "/host:/container",
            "--env",
            "FOO=bar",
            "ghcr.io/org/image:latest",
        ]
        assert epi.find_image(argv, self.FLAGS_NO_VALUE) == "ghcr.io/org/image:latest"

    def test_not_podman_run(self) -> None:
        argv = ["podman", "pull", "alpine"]
        assert epi.find_image(argv, self.FLAGS_NO_VALUE) is None

    def test_no_image_all_flags(self) -> None:
        argv = ["podman", "run", "--rm", "--name", "x"]
        assert epi.find_image(argv, self.FLAGS_NO_VALUE) is None

    def test_mixed_equals_and_space(self) -> None:
        argv = [
            "podman",
            "run",
            "--user=1000:1000",
            "--env",
            "A=1",
            "--publish",
            "8080:80",
            "-d",
            "nginx:alpine",
            "nginx",
            "-g",
            "daemon off;",
        ]
        assert epi.find_image(argv, self.FLAGS_NO_VALUE) == "nginx:alpine"

    def test_image_with_digest(self) -> None:
        ref = "docker.io/library/redis@sha256:abcdef1234567890"
        argv = ["podman", "run", "--rm", ref]
        assert epi.find_image(argv, self.FLAGS_NO_VALUE) == ref

    def test_empty_flags_no_value_treats_all_as_value_takers(self) -> None:
        argv = ["podman", "run", "--rm", "alpine"]
        assert epi.find_image(argv, set()) is None

    def test_realistic_service_line(self) -> None:
        argv = [
            "/usr/bin/podman",
            "run",
            "--name",
            "transmission",
            "--replace",
            "--rm",
            "--sdnotify=healthy",
            "--log-driver=journald",
            "--network",
            "slirp4netns:allow_host_loopback=true",
            "--user",
            "1001:1001",
            "--volume",
            "/mnt/services/transmission:/config",
            "--publish",
            "127.0.0.1:9091:9091",
            "--health-cmd",
            "curl -sf http://localhost:9091",
            "--health-startup-cmd",
            "curl -sf http://localhost:9091",
            "--health-startup-interval",
            "1s",
            "--health-startup-retries",
            "30",
            "lscr.io/linuxserver/transmission:4.0.6",
        ]
        flags_no_value = {"--replace", "--rm", "-d", "--detach", "--init"}
        assert epi.find_image(argv, flags_no_value) == "lscr.io/linuxserver/transmission:4.0.6"
