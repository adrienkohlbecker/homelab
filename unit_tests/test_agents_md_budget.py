from pathlib import Path

ROOT = Path(__file__).parents[1]

# Codex stops reading project instructions at project_doc_max_bytes (32 KiB by
# default) and silently drops the tail -- Production and Vault ids among it.
# Keep headroom below that limit.
BUDGET_BYTES = 30 * 1024


def test_agents_md_fits_the_codex_instruction_budget() -> None:
    size = (ROOT / "AGENTS.md").stat().st_size

    assert size < BUDGET_BYTES, f"AGENTS.md is {size} bytes; trim it below {BUDGET_BYTES}"
