"""Unit tests for mise-tasks/roles/complexity.py."""

import yaml
from conftest import load_repo_module

complexity = load_repo_module("mise-tasks/roles/complexity.py", name="roles_complexity")


def test_count_tasks_recurses_blocks_and_counts_each_when_entry() -> None:
    tasks = yaml.safe_load(
        """
- name: plain
  ansible.builtin.debug:
- name: guarded block
  when: [a, b]
  block:
    - name: inner
      when: c
      ansible.builtin.debug:
  rescue:
    - name: recover
      ansible.builtin.debug:
"""
    )
    assert complexity.count_tasks(tasks) == (3, 3)


def test_count_tasks_ignores_non_list_files() -> None:
    assert complexity.count_tasks(None) == (0, 0)
    assert complexity.count_tasks({"key": "value"}) == (0, 0)


def test_rank_orders_by_mean_percentile_and_shares_ties() -> None:
    row = complexity.RoleComplexity
    ranked = complexity.rank([row("small", 1, 1, 0, 0), row("big", 9, 9, 9, 9), row("mid", 5, 5, 0, 0)])
    assert [r["role"] for r in ranked] == ["big", "mid", "small"]
    assert [r["rank"] for r in ranked] == [1, 2, 3]
    # small and mid tie on whens and cells, so both get the mid-rank 0.25 there.
    assert ranked[2]["score"] == round((0 + 0 + 0.25 + 0.25) / 4, 3)
