"""Tests for `bench lint-fixtures --check az` (issue #151)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema.validators import validator_for

from bench.cli import main
from bench.fixture_leaks import MAX_FIXTURE_LINES
from bench.lint_fixtures import (
    AZ_PREFIX_SCHEMAS,
    ERROR,
    SKIPPED,
    WARNING,
    Finding,
    check_az_fixtures,
    match_az_schema,
)

# A minimal valid `az aks show` payload grounded in the corpus template task.
VALID_AKS_SHOW = {
    "id": "/subscriptions/00000000-0000-0000-0000-000000000002/resourcegroups/rg-x/providers/Microsoft.ContainerService/managedClusters/aks-x",
    "name": "aks-x",
    "location": "uksouth",
    "dnsPrefix": "aks-x",
    "provisioningState": "Succeeded",
    "kubernetesVersion": "1.29.7",
    "networkProfile": {"networkPlugin": "azure"},
    "agentPoolProfiles": [
        {"name": "nodepool1", "vmSize": "Standard_D4s_v5", "provisioningState": "Succeeded"}
    ],
}


@pytest.fixture
def task_tree(tmp_path: Path) -> Path:
    """Build a tasks/<category>/<task>/fixtures tree with one valid az fixture."""
    fixtures = tmp_path / "aks-identity" / "task-x" / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "az-aks-show.json").write_text(
        json.dumps(VALID_AKS_SHOW), encoding="utf-8"
    )
    return tmp_path


def _find(findings: list[Finding], path_suffix: str) -> list[Finding]:
    return [f for f in findings if f.path.endswith(path_suffix)]


def test_valid_fixture_yields_no_findings(task_tree: Path) -> None:
    assert check_az_fixtures(task_tree) == []


def test_missing_required_field_warns(task_tree: Path) -> None:
    fixtures = task_tree / "aks-identity" / "task-x" / "fixtures"
    broken = dict(VALID_AKS_SHOW)
    del broken["provisioningState"]
    (fixtures / "az-aks-show.json").write_text(json.dumps(broken), encoding="utf-8")
    findings = check_az_fixtures(task_tree)
    assert len(findings) == 1
    assert findings[0].severity == WARNING
    assert "provisioningState" in findings[0].message
    assert "aks-show.json" in findings[0].message


def test_malformed_json_warns_not_fails(task_tree: Path) -> None:
    fixtures = task_tree / "aks-identity" / "task-x" / "fixtures"
    (fixtures / "az-aks-show.json").write_text("{not json", encoding="utf-8")
    findings = check_az_fixtures(task_tree)
    assert len(findings) == 1
    assert findings[0].severity == WARNING
    assert "not valid JSON" in findings[0].message


def test_unknown_az_prefix_skipped_not_failed(task_tree: Path) -> None:
    fixtures = task_tree / "aks-identity" / "task-x" / "fixtures"
    (fixtures / "az-acr-show.json").write_text("{}", encoding="utf-8")
    findings = check_az_fixtures(task_tree)
    skipped = _find(findings, "az-acr-show.json")
    assert len(skipped) == 1
    assert skipped[0].severity == SKIPPED
    assert all(f.severity != ERROR for f in findings)


def test_non_az_json_ignored(task_tree: Path) -> None:
    fixtures = task_tree / "aks-identity" / "task-x" / "fixtures"
    (fixtures / "alert-history.json").write_text("[]", encoding="utf-8")
    (fixtures / "kubectl-logs.txt").write_text("not json", encoding="utf-8")
    assert check_az_fixtures(task_tree) == []


def test_longest_prefix_maps_suffix_variants(task_tree: Path) -> None:
    """`-pool2` / `-acr` style variants reuse the parent command's schema."""
    fixtures = task_tree / "aks-identity" / "task-x" / "fixtures"
    (fixtures / "az-network-vnet-subnet-show-pool2.json").write_text(
        json.dumps(
            {
                "name": "snet-pool2",
                "addressPrefix": "10.224.1.0/24",
                "provisioningState": "Succeeded",
                "resourceGroup": "rg-x",
            }
        ),
        encoding="utf-8",
    )
    (fixtures / "az-role-assignment-list-acr.json").write_text("[]", encoding="utf-8")
    findings = check_az_fixtures(task_tree)
    assert _find(findings, "az-network-vnet-subnet-show-pool2.json") == []
    assert _find(findings, "az-role-assignment-list-acr.json") == []
    assert match_az_schema("az-role-assignment-list-acr").name == "role-assignment-list.json"
    assert match_az_schema("az-network-vnet-subnet-show-pool2").name == "vnet-subnet-show.json"


def test_match_az_schema_mapping() -> None:
    assert match_az_schema("az-aks-show") is not None
    assert match_az_schema("az-aks-nodepool-list").name == "aks-nodepool-list.json"
    # `az-aks-show` must not swallow `az-aks-nodepool-list` (distinct prefixes).
    assert (
        match_az_schema("az-aks-nodepool-list") != match_az_schema("az-aks-show")
    )


def test_every_mapped_schema_is_valid_json_schema() -> None:
    for _prefix, rel in AZ_PREFIX_SCHEMAS:
        schema = json.loads((Path(__file__).parents[1] / "bench" / "schemas" / rel).read_text())
        cls = validator_for(schema)
        cls.check_schema(schema)


def test_real_corpus_has_no_error_findings() -> None:
    """Corpus smoke test: real fixtures may warn (reported in CI), never error."""
    repo_root = Path(__file__).parents[1]
    findings = check_az_fixtures(repo_root / "tasks")
    assert [f for f in findings if f.severity == ERROR] == []


def test_cli_exits_zero_when_only_warnings(task_tree: Path, capsys) -> None:
    fixtures = task_tree / "aks-identity" / "task-x" / "fixtures"
    (fixtures / "az-acr-show.json").write_text("{}", encoding="utf-8")
    rc = main(["lint-fixtures", "--check", "az", "--tasks-root", str(task_tree)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "SKIPPED" in out
    assert "warning-only" in out or "az:" in out


def test_cli_exits_one_on_error_findings(tmp_path: Path, capsys) -> None:
    """Blocking gate (issue #155): an ERROR finding fails the all-checkers run.

    An oversized fixture trips the `size` checker's line ceiling, which is
    ERROR-severity; with warnings alone the run still exits zero (see
    test_cli_exits_zero_when_only_warnings). No ``--check`` so this mirrors
    the consolidated blocking step in CI exactly.
    """
    fixtures = tmp_path / "aks-identity" / "task-y" / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "kubectl-logs-pod.txt").write_text(
        "\n".join(f"log line {n}" for n in range(MAX_FIXTURE_LINES + 1)),
        encoding="utf-8",
    )
    rc = main(["lint-fixtures", "--tasks-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "ERROR" in out
    assert "exceeds the" in out


def test_cli_rejects_unknown_check(task_tree: Path) -> None:
    with pytest.raises(SystemExit):
        main(["lint-fixtures", "--check", "nonexistent", "--tasks-root", str(task_tree)])


# ---------------------------------------------------------------------------
# logs check (issue #153): timestamps and log realism
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402  (grouped with the logs tests)

from bench.lint_fixtures import (  # noqa: E402
    INCIDENT_WINDOW,
    WARNING,
    check_logs,
    is_log_file,
    parse_age_token,
    parse_iso_timestamp,
)


def log_task(root: Path, task_id: str = "monitoring-041", files: dict | None = None) -> Path:
    """Scaffold a minimal task directory with the given fixture files."""
    fixtures = root / task_id / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    (root / task_id / "task.yaml").write_text(f"id: {task_id}\n")
    for name, content in (files or {}).items():
        (fixtures / name).write_text(content)
    return root


def findings_for(tasks_root: Path) -> list[Finding]:
    return check_logs(tasks_root)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-30T06:00:12.481Z INFO", "2026-10-30 06:00:12.481000+00:00"),
        ("2026-10-30T06:00:12Z", "2026-10-30 06:00:12+00:00"),
        # KQL emits seven fractional digits.
        ("2026-10-20T00:00:00.0000000Z,49231", "2026-10-20 00:00:00+00:00"),
        # PostgreSQL-style space separator and UTC zone name.
        ("2026-10-30 09:41:02.117 UTC [1023] LOG:", "2026-10-30 09:41:02.117000+00:00"),
        ("2026-10-30 05:57:33Z", "2026-10-30 05:57:33+00:00"),
        ("2026-10-30T06:00:00+02:00 event", "2026-10-30 04:00:00+00:00"),
    ],
)
def test_logs_parse_iso_timestamp_accepts_corpus_formats(text, expected):
    assert str(parse_iso_timestamp(text)) == expected


@pytest.mark.parametrize(
    "text",
    [
        "no timestamp here",
        "2026-13-40T25:00:00Z",  # plausible shape, impossible date
        "2026-02-30T00:00:00Z",  # February 30
    ],
)
def test_logs_parse_iso_timestamp_rejects_invalid(text):
    assert parse_iso_timestamp(text) is None


@pytest.mark.parametrize(
    ("text", "expected_seconds"),
    [
        ("13m", 780),
        ("2m34s", 154),
        ("6d2h", 6 * 86400 + 7200),
        ("3h", 3 * 3600),
        ("8000ms", 8),
        ("45s", 45),
        ("2y", 2 * 365 * 86400),
    ],
)
def test_logs_parse_age_token(text, expected_seconds):
    age = parse_age_token(text)
    assert age is not None
    assert age.total_seconds() == expected_seconds


@pytest.mark.parametrize("text", ["", "12x", "soon", "2m34"])
def test_logs_parse_age_token_rejects_invalid(text):
    # "2m34" (missing unit) is not something kubectl prints.
    assert parse_age_token(text) is None


def test_logs_is_log_file():
    assert is_log_file("kubectl-logs-orders-api.txt")
    assert is_log_file("billing-api.log")
    assert is_log_file("pipeline-log.txt")
    assert not is_log_file("kubectl-get-pods.txt")
    assert not is_log_file("kubectl-describe-pod.txt")


def test_logs_timestamped_line_without_level_is_flagged(tmp_path):
    root = log_task(
        tmp_path,
        files={"kubectl-logs-app.txt": "2026-10-30T06:00:12Z doing things\n"},
    )
    findings = findings_for(root)
    assert any("no severity level" in f.message for f in findings)
    assert all(f.severity == WARNING for f in findings)


def test_logs_timestamped_line_with_level_passes(tmp_path):
    root = log_task(
        tmp_path,
        files={
            "kubectl-logs-app.txt": "2026-10-30T06:00:12Z INFO doing things\n",
            "app.log": "2026-10-30 06:00:12.1 UTC [1] ERROR kaput\n",
            "ama-logs.txt": "2026-10-30T06:00:12Z [info] heartbeat ok\n",
        },
    )
    assert findings_for(root) == []


def test_logs_envoy_native_severity_passes_level_check(tmp_path):
    # istio-proxy logs Envoy's native format: a lowercase severity word right
    # after the ISO timestamp (issue #156, aks-networking-041).
    root = log_task(
        tmp_path,
        files={
            "istio-proxy.log": (
                "2026-10-28T09:14:02.318Z warning envoy connection [C2174] "
                "TLS handshake failed: wrong version number, peer 10.240.5.23:48214\n"
                "2026-10-28T09:14:15.002Z info    envoy connection [C2205] "
                "TLS handshake complete: TLSv1.3\n"
                "2026-10-28T09:14:16.118Z debug   envoy connection [C2174] "
                "socket closed: transport handshake failure\n"
            ),
        },
    )
    assert findings_for(root) == []


def test_logs_lowercase_error_in_message_text_still_flagged(tmp_path):
    # "error" inside the message body is not level evidence — only a severity
    # word directly after the timestamp counts for the Envoy-style pattern.
    root = log_task(
        tmp_path,
        files={
            "app.log": "2026-10-30T06:00:12Z SSL routines::error:0A00010A wrong version\n",
        },
    )
    findings = findings_for(root)
    assert any("no severity level" in f.message for f in findings)


def test_logs_ado_pipeline_transcript_is_exempt_from_level_check(tmp_path):
    transcript = (
        "2026-10-30T06:02:10.1173411Z ##[section]Starting: init\n"
        "2026-10-30T06:02:10.4420981Z Agent name: ubuntu-latest-agent-03\n"
        "2026-10-30T06:02:11.6093333Z ##[command]terraform init\n"
    )
    root = log_task(tmp_path, files={"pipeline-log.txt": transcript})
    assert findings_for(root) == []


def test_logs_kql_result_csv_never_needs_a_level(tmp_path):
    csv = "TimeGenerated,request_count\n2026-10-30T14:00:00Z,4790\n"
    root = log_task(tmp_path, files={"kql-5xx.csv": csv})
    assert findings_for(root) == []


def test_logs_out_of_order_timestamps_within_a_stream_are_flagged(tmp_path):
    log = (
        "2026-10-30T06:00:12Z INFO first\n"
        "2026-10-30T06:00:10Z INFO goes backwards\n"
    )
    root = log_task(tmp_path, files={"kubectl-logs-app.txt": log})
    assert any("not monotonic" in f.message for f in findings_for(root))


def test_logs_stream_break_resets_ordering_baseline(tmp_path):
    log = (
        "# kubectl logs pod-a\n"
        "2026-10-30T06:01:00Z INFO later\n"
        "# kubectl logs pod-b\n"
        "2026-10-30T06:00:00Z INFO earlier but a different stream\n"
    )
    root = log_task(tmp_path, files={"kubectl-logs-app.txt": log})
    assert findings_for(root) == []


def test_logs_event_times_a_year_apart_across_files_are_flagged(tmp_path):
    files = {
        "kubectl-logs-app.txt": "2026-10-30T06:00:12Z INFO now\n",
        "kubectl-logs-app-old.txt": "2025-10-30T06:00:12Z INFO a year ago\n",
    }
    root = log_task(tmp_path, files=files)
    assert any("incident window" in f.message for f in findings_for(root))


def test_logs_event_times_within_the_window_pass(tmp_path):
    days_back = (INCIDENT_WINDOW - timedelta(hours=1)).days
    files = {
        "kubectl-logs-app.txt": "2026-10-30T06:00:12Z INFO a\n",
        "kubectl-logs-old.txt": (
            f"2026-10-{30 - days_back:02d}T06:00:12Z INFO b\n"
        ),
    }
    root = log_task(tmp_path, files=files)
    assert findings_for(root) == []


def test_logs_midnight_bins_are_excluded_from_the_window(tmp_path):
    csv = "TimeBin,RowCount\n" + "".join(
        f"2026-09-{day:02d}T00:00:00.0000000Z,{day}\n" for day in range(1, 29)
    )
    root = log_task(tmp_path, files={"kql-daily-count.csv": csv})
    assert findings_for(root) == []


def test_logs_klog_headers_without_a_year_do_not_manufacture_a_window_span(tmp_path):
    # Klog headers carry no year; their synthetic placeholder year must not be
    # compared against ISO dates in other files (issue #159: aks-ops-050).
    files = {
        "konnectivity-agent.log": (
            "I1028 02:14:09.113742       1 client.go:761] \"Connecting to server\"\n"
            "E1028 02:16:40.160901       1 service.go:120] \"Lost connection\"\n"
        ),
        "azfw-log.csv": "TimeGenerated,Action\n2026-10-28T01:32:09Z,Deny\n",
    }
    root = log_task(tmp_path, files=files)
    assert findings_for(root) == []


def test_logs_describe_events_with_fake_reason_are_flagged(tmp_path):
    events = (
        "Events:\n"
        "  Type     Reason     Age                From\n"
        "  ----     ------     ----               ----\n"
        "  Normal   TotallyMadeUp   13m   kubelet   Made-up reason\n"
        "  Warning  FailedScheduling   12m   scheduler   0/4 nodes available\n"
    )
    root = log_task(tmp_path, files={"kubectl-describe-pod.txt": events})
    messages = [f.message for f in findings_for(root)]
    assert any("unknown Kubernetes event reason 'TotallyMadeUp'" in m for m in messages)
    assert not any("FailedScheduling" in m for m in messages)


def test_logs_get_events_with_bad_age_is_flagged(tmp_path):
    events = (
        "NAMESPACE   LAST SEEN   TYPE      REASON        OBJECT      MESSAGE\n"
        "payments    soon-ish    Warning   Evicted       pod/x-1     Node was low\n"
        "payments    29m         Warning   Evicted       pod/x-2     Node was low\n"
    )
    root = log_task(tmp_path, files={"kubectl-get-events.txt": events})
    assert any(
        "unparseable kubectl event age 'soon-ish'" in f.message
        for f in findings_for(root)
    )


def test_logs_unparseable_iso_timestamp_is_flagged(tmp_path):
    root = log_task(
        tmp_path,
        files={"kubectl-logs-app.txt": "2026-13-40T25:00:00Z INFO nonsense\n"},
    )
    assert any("unparseable ISO 8601" in f.message for f in findings_for(root))


def test_logs_clean_corpus_cli_exits_zero(tmp_path, capsys):
    root = log_task(
        tmp_path,
        files={"kubectl-logs-app.txt": "2026-10-30T06:00:12Z INFO fine\n"},
    )
    assert main(["lint-fixtures", "--check", "logs", "--tasks-root", str(root)]) == 0
    # print_report emits nothing when there is nothing to report.
    assert capsys.readouterr().out == ""


def test_logs_warning_findings_keep_cli_exit_zero(tmp_path, capsys):
    # Warnings surface in the report but do not fail the run (issue #153:
    # warning-only in CI; issue #155 owns promoting checks to blocking).
    root = log_task(
        tmp_path,
        files={"kubectl-logs-app.txt": "2026-10-30T06:00:12Z no level\n"},
    )
    assert main(["lint-fixtures", "--check", "logs", "--tasks-root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "logs: 1 fixture finding(s), 1 warning(s), 0 skipped, 0 error(s)" in out
