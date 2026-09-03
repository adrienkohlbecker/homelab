"""Default Ansible stdout callback with noisy result payloads summarized."""

import json

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

_STATUS_KEEP = ("ActiveState", "SubState", "Result")
_STAT_KEEP = (
    "exists",
    "path",
    "isdir",
    "isreg",
    "islnk",
    "lnk_target",
    "mode",
    "executable",
    "pw_name",
    "gr_name",
    "size",
    "mtime",
    "checksum",
)
_JSON_DIGEST_THRESHOLD = 2048
_JSON_KEY_LIMIT = 10


def _json_summary(value):
    encoded = json.dumps(value, separators=(",", ":"))
    if len(encoded) <= _JSON_DIGEST_THRESHOLD:
        return value
    if isinstance(value, dict):
        keys = sorted(value)[:_JSON_KEY_LIMIT]
        suffix = ", ..." if len(value) > len(keys) else ""
        shape = f"object with keys: {', '.join(keys)}{suffix}"
    elif isinstance(value, list):
        shape = f"array with {len(value)} items"
    else:
        shape = type(value).__name__
    return f"<{len(encoded)}-character JSON {shape} hidden>"


class CallbackModule(DefaultCallback):
    CALLBACK_NAME = "digest"

    def _dump_results(self, result, *args, **kwargs):
        def digest(obj):
            if isinstance(obj, dict):
                out = {}
                is_json_http_response = {"json", "status", "url"} <= obj.keys()
                for key, value in obj.items():
                    if is_json_http_response and key == "json":
                        value = _json_summary(value)
                    elif is_json_http_response and key == "content":
                        continue
                    elif key == "status" and isinstance(value, dict) and "ActiveState" in value:
                        value = {k: value[k] for k in _STATUS_KEEP if k in value}
                    elif key == "stat" and isinstance(value, dict) and "exists" in value:
                        value = {k: value[k] for k in _STAT_KEEP if k in value}
                    else:
                        value = digest(value)
                    out[key] = value
                return out
            if isinstance(obj, list):
                return [digest(item) for item in obj]
            return obj

        return super()._dump_results(digest(result), *args, **kwargs)
