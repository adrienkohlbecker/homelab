"""Stdout callback: the stock `default` output with the two result keys that
dominate `-v` logs collapsed to a short summary.

Measured on a real converge transcript, the systemd module's `status` (the full
`systemctl show` dump, ~250 keys per restart) alone is ~half of all output
lines - both at the top level and nested where the `systemd_unit` helper
persists it via `set_fact`. Everything else operators rely on at `-v` - command
`stdout`/`stderr`, file diffs, changed attributes - is left untouched. Large
`ansible_facts` blobs (an explicit `setup:`/`service_facts`, not the implicit
gather, which does not dump at `-v`) are collapsed too. Keeps CI job logs under
GitLab's size cap.

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

# ansible_facts dicts larger than this are gather/custom-fact dumps, not
# set_fact results - collapse those to a one-line key list, show small ones in
# full so set_fact output stays readable.
_FACTS_DIGEST_THRESHOLD = 25


def _digest_status(obj):
    """Return a copy of *obj* with every systemd `status` dict collapsed.

    The dict surfaces both at the top level (the systemd task itself) and
    nested - the `systemd_unit` helper persists the registered result via
    `set_fact`, so it reappears under `ansible_facts.<unit>_started_result`
    and inside loop `results`. Recurse so it is caught wherever it lands.
    """
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key == "status" and isinstance(value, dict) and _SYSTEMD_MARKER in value:
                out[key] = {k: value[k] for k in _STATUS_KEEP if k in value}
            else:
                out[key] = _digest_status(value)
        return out
    if isinstance(obj, list):
        return [_digest_status(item) for item in obj]
    return obj


class CallbackModule(DefaultCallback):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "digest"

    def _dump_results(self, result, *args, **kwargs):
        # _digest_status rebuilds the structure, so the registered result is
        # never mutated and later mutation here is safe.
        result = _digest_status(result)
        facts = result.get("ansible_facts")
        if isinstance(facts, dict) and len(facts) > _FACTS_DIGEST_THRESHOLD:
            result["ansible_facts"] = f"<{len(facts)} facts hidden: {', '.join(sorted(facts))}>"
        return super()._dump_results(result, *args, **kwargs)
