"""Stdout callback matching default output with verbose result fields summarized.

Keeps command stdout/stderr and diffs intact, but shortens systemd ``status``,
``stat`` results, duplicate diffs persisted under ``ansible_facts``, and large
fact-gather blobs. The documentation fragments keep ``callback_result_format``
available for this derived callback.
"""

from ansible.plugins.callback.default import CallbackModule as DefaultCallback

DOCUMENTATION = """
  name: digest
  type: stdout
  short_description: default output with verbose module result dicts digested
  description:
    - Default callback output with noisy module result payloads summarized.
  extends_documentation_fragment:
    - default_callback
    - result_format_callback
"""

_STATUS_KEEP = ("Id", "ActiveState", "SubState", "Result", "ExecMainPID")
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
_FACTS_DIGEST_THRESHOLD = 25


class CallbackModule(DefaultCallback):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "digest"

    def _dump_results(self, result, *args, **kwargs):
        def is_diff(value):
            if isinstance(value, dict):
                return bool({"before", "after", "prepared"} & value.keys())
            if isinstance(value, list):
                return bool(value) and all(is_diff(item) for item in value)
            return False

        def digest(obj, in_facts=False):
            if isinstance(obj, dict):
                out = {}
                for key, value in obj.items():
                    if in_facts and key == "diff" and is_diff(value):
                        continue
                    if key == "status" and isinstance(value, dict) and "ActiveState" in value:
                        value = {k: value[k] for k in _STATUS_KEEP if k in value}
                    elif key == "stat" and isinstance(value, dict) and "exists" in value:
                        value = {k: value[k] for k in _STAT_KEEP if k in value}
                    else:
                        value = digest(value, in_facts or key == "ansible_facts")
                    out[key] = value
                return out
            if isinstance(obj, list):
                return [digest(item, in_facts) for item in obj]
            return obj

        result = digest(result)
        facts = result.get("ansible_facts") if isinstance(result, dict) else None
        if isinstance(facts, dict) and len(facts) > _FACTS_DIGEST_THRESHOLD:
            result["ansible_facts"] = f"<{len(facts)} facts hidden: {', '.join(sorted(facts))}>"
        return super()._dump_results(result, *args, **kwargs)
