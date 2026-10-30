"""Tests for `bench lint-fixtures --check az` (issue #151)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema.validators import validator_for

from bench.cli import main
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


def test_cli_rejects_unknown_check(task_tree: Path) -> None:
    with pytest.raises(SystemExit):
        main(["lint-fixtures", "--check", "nonexistent", "--tasks-root", str(task_tree)])
