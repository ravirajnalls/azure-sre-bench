"""Tests for the fixture lint — answer leaks and size limits (issue #154)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bench.cli import main
from bench.fixture_leaks import lint_fixtures

DATA_TASKS = Path(__file__).parent / "data" / "tasks"
BASE_TASK = DATA_TASKS / "monitoring" / "monitoring-001"


def copy_task(tmp_path: Path, name: str = "monitoring-001") -> Path:
    dest = tmp_path / "tasks" / "monitoring" / name
    shutil.copytree(BASE_TASK, dest)
    return dest


def load_rubric(task_dir: Path) -> dict:
    import json

    return json.loads((task_dir / "expected.json").read_text(encoding="utf-8"))


def test_clean_task_passes(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    assert lint_fixtures(task_dir, load_rubric(task_dir)) == []


def test_size_limit_fires_at_151_lines(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    fixture = task_dir / "fixtures" / "kubectl-logs-ingress.txt"
    fixture.write_text("\n".join(f"line {n}" for n in range(151)) + "\n")
    errors = lint_fixtures(task_dir, load_rubric(task_dir))
    assert any("kubectl-logs-ingress.txt" in e and "151 lines" in e for e in errors)


def test_size_limit_at_ceiling_passes(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    fixture = task_dir / "fixtures" / "kubectl-logs-ingress.txt"
    fixture.write_text("\n".join(f"line {n}" for n in range(150)) + "\n")
    assert lint_fixtures(task_dir, load_rubric(task_dir)) == []


def test_max_lines_is_configurable(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    for fixture_name in ("kubectl-logs-ingress.txt", "az-monitor-metrics.json"):
        fixture = task_dir / "fixtures" / fixture_name
        fixture.write_text("\n".join(f"line {n}" for n in range(11)) + "\n")
    assert lint_fixtures(task_dir, load_rubric(task_dir), max_lines=10)
    assert lint_fixtures(task_dir, load_rubric(task_dir), max_lines=11) == []


def test_fixture_count_enforced(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    for n in range(9):  # 2 existing + 9 = 11 files
        (task_dir / "fixtures" / f"extra-{n}.txt").write_text("x\n")
    errors = lint_fixtures(task_dir, load_rubric(task_dir))
    assert any("must contain 2-10 files, found 11" in e for e in errors)


def test_commands_file_exempt_from_count(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    (task_dir / "fixtures" / "_commands.yaml").write_text("commands: []\n")
    assert lint_fixtures(task_dir, load_rubric(task_dir)) == []


def test_hint_word_in_comment_fires(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    fixture = task_dir / "fixtures" / "kubectl-logs-ingress.txt"
    fixture.write_text("# TODO: fix the misconfigured upstream\nserver block\n")
    errors = lint_fixtures(task_dir, load_rubric(task_dir))
    joined = "\n".join(errors)
    assert "answer-hint word 'todo'" in joined
    assert "answer-hint word 'misconfigured'" in joined
    assert ":1:" in joined


def test_hint_word_matches_whole_words_only(tmp_path: Path):
    """`bug` must not fire inside DEBUG tokens (word-boundary matching)."""
    task_dir = copy_task(tmp_path)
    fixture = task_dir / "fixtures" / "kubectl-logs-ingress.txt"
    fixture.write_text("DEBUG_LOG_LEVEL=info\naudit-debug tail\n")
    assert lint_fixtures(task_dir, load_rubric(task_dir)) == []
    fixture.write_text("# BUG: wrong upstream\n")
    assert any("answer-hint word 'bug'" in e for e in lint_fixtures(task_dir, load_rubric(task_dir)))


def test_hint_word_in_filename_fires(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    leaked = task_dir / "fixtures" / "root-cause.txt"
    shutil.move(task_dir / "fixtures" / "kubectl-logs-ingress.txt", leaked)
    errors = lint_fixtures(task_dir, load_rubric(task_dir))
    assert any("root-cause.txt" in e and "'root cause'" in e for e in errors)


def test_comment_stating_root_cause_fires(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    fixture = task_dir / "fixtures" / "kubectl-logs-ingress.txt"
    fixture.write_text("# the ingress saturation proves backend capacity is exhausted\nlogs:\n")
    errors = lint_fixtures(task_dir, load_rubric(task_dir))
    assert any("comment states the rubric alternative" in e and "ingress saturation" in e for e in errors)


def test_comment_stating_fix_fires(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    fixture = task_dir / "fixtures" / "kubectl-logs-ingress.txt"
    fixture.write_text("# fix: scale the ingress controller\nlogs:\n")
    assert any("scale the ingress controller" in e for e in lint_fixtures(task_dir, load_rubric(task_dir)))


def test_command_echo_comment_does_not_fire(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    fixture = task_dir / "fixtures" / "kubectl-logs-ingress.txt"
    # "peak traffic" is a rubric alternative, but inside a command echo —
    # which may be capitalized (regression: `# Grafana panel:` leaked through).
    fixture.write_text(
        "# kubectl get deploy -n ingress --sort-by peak-traffic\n"
        "# Grafana panel: pricing-api — peak traffic per pod\n"
        "logs:\n"
    )
    assert lint_fixtures(task_dir, load_rubric(task_dir)) == []


def test_hyphenated_identifier_in_comment_does_not_fire(tmp_path: Path):
    """A script header naming itself (`agent-replacement.sh`) is an identifier,
    not a prose statement of the fix (regression: cost-031 corpus hit)."""
    task_dir = copy_task(tmp_path)
    fixture = task_dir / "fixtures" / "kubectl-logs-ingress.txt"
    # "agent-replacement" is a rubric-accepted fix alternative, but as the
    # script's own name it is an identifier, not prose stating the fix.
    rubric = {"fix": [["agent-replacement"]]}
    fixture.write_text("# agent-replacement.sh — rolls a new controller image\nlogs:\n")
    assert lint_fixtures(task_dir, rubric) == []
    # The same alternative as prose is a leak.
    fixture.write_text("# the fix is agent-replacement\nlogs:\n")
    assert lint_fixtures(task_dir, rubric)


def test_log_format_comment_does_not_fire(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    fixture = task_dir / "fixtures" / "kubectl-logs-ingress.txt"
    fixture.write_text("# [error] upstream saturation detected at 09:20\nlogs:\n")
    assert lint_fixtures(task_dir, load_rubric(task_dir)) == []


def test_editorial_filename_with_alternative_fires(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    leaked = task_dir / "fixtures" / "wrong-upstream-saturation.txt"
    shutil.move(task_dir / "fixtures" / "kubectl-logs-ingress.txt", leaked)
    errors = lint_fixtures(task_dir, load_rubric(task_dir))
    assert any("wrong-upstream-saturation.txt" in e and "upstream saturation" in e for e in errors)


def test_evidence_named_filename_does_not_fire(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    # Named after the resource being inspected, like real evidence files.
    renamed = task_dir / "fixtures" / "upstream-saturation-metrics.json"
    shutil.move(task_dir / "fixtures" / "kubectl-logs-ingress.txt", renamed)
    assert lint_fixtures(task_dir, load_rubric(task_dir)) == []


def test_missing_rubric_still_checks_size_and_hints(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    fixture = task_dir / "fixtures" / "kubectl-logs-ingress.txt"
    fixture.write_text("# FIXME later\n" + "\n".join(f"l{n}" for n in range(200)) + "\n")
    errors = lint_fixtures(task_dir, None)
    assert any("FIXME" in e.lower() or "fixme" in e for e in errors)
    assert any("201 lines" in e for e in errors)
    assert not any("comment states the rubric" in e for e in errors)


def test_check_size_skips_leak_scan(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    fixture = task_dir / "fixtures" / "kubectl-logs-ingress.txt"
    fixture.write_text("# FIXME: upstream saturation\n")
    assert lint_fixtures(task_dir, load_rubric(task_dir), check="size") == []
    assert lint_fixtures(task_dir, load_rubric(task_dir), check="leaks")


# --- CLI ------------------------------------------------------------------


def test_cli_clean_corpus_exits_zero(tmp_path: Path, capsys):
    copy_task(tmp_path)
    assert main(["lint-fixtures", "--tasks-root", str(tmp_path / "tasks")]) == 0
    out = capsys.readouterr().out
    assert "ERROR" not in out


def test_cli_leaky_corpus_exits_one(tmp_path: Path, capsys):
    task_dir = copy_task(tmp_path)
    fixture = task_dir / "fixtures" / "kubectl-logs-ingress.txt"
    fixture.write_text("# the cause is ingress saturation\nlogs:\n")
    assert main(["lint-fixtures", "--tasks-root", str(tmp_path / "tasks")]) == 1
    out = capsys.readouterr().out
    assert "ERROR" in out and "leaks:" in out


def test_cli_check_size_ignores_leaks(tmp_path: Path, capsys):
    task_dir = copy_task(tmp_path)
    fixture = task_dir / "fixtures" / "kubectl-logs-ingress.txt"
    fixture.write_text("# FIXME: upstream saturation\n")
    assert (
        main(
            ["lint-fixtures", "--tasks-root", str(tmp_path / "tasks"), "--check", "size"]
        )
        == 0
    )


def test_cli_max_lines_flag(tmp_path: Path, capsys):
    copy_task(tmp_path)
    assert (
        main(
            [
                "lint-fixtures",
                "--tasks-root",
                str(tmp_path / "tasks"),
                "--max-lines",
                "1",
            ]
        )
        == 1
    )
    assert "exceeds the 1-line fixture limit" in capsys.readouterr().out


def test_checkers_are_registered(tmp_path: Path):
    """`leaks` and `size` join the registry alongside `az` (#151), `consistency` (#152), and `logs` (#460)."""
    from bench.lint_fixtures import CHECKS, Finding, run_checks

    assert set(CHECKS) == {"az", "consistency", "leaks", "logs", "size"}
    task_dir = copy_task(tmp_path)
    fixture = task_dir / "fixtures" / "kubectl-logs-ingress.txt"
    fixture.write_text("# the cause is ingress saturation\nlogs:\n")
    findings = run_checks(tmp_path / "tasks", ["leaks"])
    assert findings and all(
        isinstance(f, Finding) and f.severity == "error" and f.check == "leaks"
        for f in findings
    )


# --- Real corpus (error-level enforcement, issue #154) --------------------


def _discovered_task_dirs() -> list[Path]:
    root = Path("tasks")
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.glob("*/*") if (path / "task.yaml").is_file()
    )


def _task_label(task_dir: Path) -> str:
    return f"{task_dir.parent.name}/{task_dir.name}"


@pytest.mark.parametrize("task_dir", _discovered_task_dirs(), ids=_task_label)
def test_real_task_fixtures_are_clean(task_dir: Path):
    """Every shipped task must pass the leak and size lint (error-level)."""
    import json

    import yaml

    from bench.heldout import SPLIT_HELDOUT, heldout_key, load_hidden_json, split_of

    config = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8")) or {}
    if split_of(config) == SPLIT_HELDOUT:
        if not heldout_key():
            pytest.skip(f"{_task_label(task_dir)} is held-out; set HELDOUT_KEY to lint it")
        rubric = load_hidden_json(task_dir, "expected.json", config=config)
    else:
        rubric = json.loads((task_dir / "expected.json").read_text(encoding="utf-8"))
    errors = lint_fixtures(task_dir, rubric)
    assert errors == [], (
        f"{_task_label(task_dir)} failed the fixture lint:\n"
        + "\n".join(f"  - {error}" for error in errors)
    )
