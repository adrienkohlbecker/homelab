"""Stdout callback: the stock `default` output with the two result keys that
dominate `-v` logs collapsed to a short summary.

Measured on a real converge transcript, the systemd module's `status` (the full
`systemctl show` dump, ~250 keys per restart) alone is ~half of all output
lines - both at the top level and nested where the `systemd_unit` helper
persists it via `set_fact`. Everything else operators rely on at `-v` - command
`stdout`/`stderr`, file diffs, changed attributes - is left untouched. The
callback also: collapses `stat` dicts to their meaningful fields (wherever they
appear - the stat task, a `set_fact`, loop items); drops the `diff` re-printed
inside a persisted file result (already shown when the original task ran); and
folds large `ansible_facts` blobs (an explicit `setup:`/`service_facts`, not the
implicit gather, which does not dump at `-v`) to a key list. Keeps CI job logs
under GitLab's size cap.

Wired via `stdout_callback = digest` + `callback_plugins = callback_plugins` in
ansible.cfg. The `extends_documentation_fragment` block is load-bearing: without
it ansible would not load the `result_format` option for this plugin and
`callback_result_format = yaml` would silently revert to json.
"""

from __future__ import annotations

from ansible.plugins.callback.default import CallbackModule as DefaultCallback

DOCUMENTATION = """
  name: digest
  type: stdout
  short_description: default output with verbose module result dicts digested
  description:
    - Identical to the C(default) callback, but collapses the systemd module's
      C(status) dict and large C(ansible_facts) gather blobs - the two largest
      contributors to C(-v) log volume - to a short summary.
  extends_documentation_fragment:
    - default_callback
    - result_format_callback
"""

# systemd/service `status` fields worth keeping; the other ~250 are noise.
_STATUS_KEEP = ("Id", "ActiveState", "SubState", "Result", "ExecMainPID")
# Every systemd `status` dict carries this; guards against collapsing an
# unrelated `status` key (e.g. a module returning its own small status dict).
_SYSTEMD_MARKER = "ActiveState"

# `stat` result fields worth keeping (the human-meaningful ones, matching what
# roles actually reference); drops the ~15 permission-bit booleans mode already
# encodes, the a/c-time stamps, and dev/inode/nlink/uid/gid/block noise. Every
# stat dict carries `exists`, which doubles as the marker.
_STAT_KEEP = (
    "exists",
    "path",
    "isdir",
    "isreg",
    "islnk",
    "lnk_source",
    "lnk_target",
    "mode",
    "executable",
    "pw_name",
    "gr_name",
    "size",
    "mtime",
    "checksum",
    "mimetype",
)

# ansible_facts dicts larger than this are gather/custom-fact dumps, not
# set_fact results - collapse those to a one-line key list, show small ones in
# full so set_fact output stays readable.
_FACTS_DIGEST_THRESHOLD = 25


def _is_diff(value):
    """True if *value* is an ansible task diff (dict, or list of them)."""
    if isinstance(value, dict):
        return any(k in value for k in ("before", "after", "prepared"))
    if isinstance(value, list):
        return bool(value) and all(_is_diff(item) for item in value)
    return False


def _digest(obj, in_facts=False):
    """Return a copy of *obj* with the two `-v` log hogs collapsed.

    - systemd `status` dicts -> their summary fields, wherever they appear:
      at the top level (the systemd task) and nested where the `systemd_unit`
      helper persists the registered result via `set_fact`, so it reappears
      under `ansible_facts.<unit>_started_result` and inside loop `results`.
    - the `diff` carried by a file-module result that was registered and
      persisted via `set_fact` (so it lands under `ansible_facts`): that
      before/after was already printed when the original task ran, so drop the
      duplicate. Scoped to the `ansible_facts` subtree - a task's own top-level
      diff is rendered by the callback's diff path, not this result dict.
    - `stat` dicts -> their human-meaningful fields, wherever they appear: the
      stat task itself, persisted via `set_fact`, and per-item in loops.
    """
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if in_facts and key == "diff" and _is_diff(value):
                continue
            if key == "status" and isinstance(value, dict) and _SYSTEMD_MARKER in value:
                out[key] = {k: value[k] for k in _STATUS_KEEP if k in value}
            elif key == "stat" and isinstance(value, dict) and "exists" in value:
                out[key] = {k: value[k] for k in _STAT_KEEP if k in value}
            else:
                out[key] = _digest(value, in_facts or key == "ansible_facts")
        return out
    if isinstance(obj, list):
        return [_digest(item, in_facts) for item in obj]
    return obj


def _collapse_large_facts(result):
    """Replace a large top-level `ansible_facts` dict with a one-line key list.

    Mutates and returns *result* - only ever called on the fresh copy `_digest`
    produces, so the registered result is untouched.
    """
    facts = result.get("ansible_facts") if isinstance(result, dict) else None
    if isinstance(facts, dict) and len(facts) > _FACTS_DIGEST_THRESHOLD:
        result["ansible_facts"] = f"<{len(facts)} facts hidden: {', '.join(sorted(facts))}>"
    return result


class CallbackModule(DefaultCallback):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "digest"

    def _dump_results(self, result, *args, **kwargs):
        return super()._dump_results(_collapse_large_facts(_digest(result)), *args, **kwargs)
