from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).parents[1] / "mise-tasks" / "ci" / "reap-qemu-images.py"
_SPEC = importlib.util.spec_from_file_location("reap_qemu_images", _MODULE_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
reaper = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = reaper
_SPEC.loader.exec_module(reaper)


def test_retired_release_does_not_keep_future_dated_build() -> None:
    build = reaper.Build(
        ubuntu="retired",
        machine="box",
        build_id="future",
        prefix="retired/box/future/",
        last_modified=dt.datetime.now(dt.UTC) + dt.timedelta(days=1),
        object_count=2,
        size_bytes=1,
    )

    reaper.mark_keep_reasons([build], promoted="future", retired=True)

    assert not build.keep
