"""Tests for bench lint-fixtures cross-file consistency (issue #152)."""

from __future__ import annotations

import json

import pytest

from bench.cli import main
from bench.fixture_lint import (
    find_conflicts,
    iter_task_dirs,
    lint_task,
    load_ignore,
)

PLACEHOLDER_SUB = "00000000-0000-0000-0000-000000000001"


def make_task(tmp_path, files: dict[str, str], task_id: str = "aks-x-001") -> str:
    """Seed a task directory with fixture files; returns the task id."""
    task_dir = tmp_path / "tasks" / "aks-x" / task_id
    (task_dir / "fixtures").mkdir(parents=True)
    for name, content in files.items():
        (task_dir / "fixtures" / name).write_text(content)
    (task_dir / "task.yaml").write_text(f"id: {task_id}\ncategory: aks-x\n")
    return task_id


def task_dir_of(tasks_root):
    """First (only) task directory under the seeded root."""
    dirs = iter_task_dirs(tasks_root)
    assert len(dirs) == 1, f"expected one task dir, got {len(dirs)}"
    return dirs[0]


def aks_show(cluster: str, location: str = "eastus", **extra) -> str:
    return json.dumps(
        {
            "id": f"/subscriptions/{PLACEHOLDER_SUB}/resourceGroups/rg-x/providers"
            "/Microsoft.ContainerService/managedClusters/{cluster}",
            "name": cluster,
            "location": location,
            "type": "Microsoft.ContainerService/managedClusters",
            "dnsPrefix": "x",
            "tenantId": "00000000-0000-0000-0000-000000000002",
            **extra,
        }
    )


def test_consistent_fixtures_produce_no_findings(tmp_path):
    task_id = make_task(
        tmp_path,
        {
            "az-aks-show.json": aks_show("aks-contoso-prod"),
            "kubectl-get-sa.yaml": (
                "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n"
                "  name: orders-api\n  namespace: orders\n"
            ),
            "logs.txt": "2026-10-30 INFO pod orders-api-abc in orders started\n",
        },
    )
    task_dir = task_dir_of(tmp_path / "tasks")
    assert lint_task(task_dir) == []
    assert main(["lint-fixtures", "--task", task_id, "--tasks-root", str(tmp_path / "tasks")]) == 0


def test_cluster_name_conflict_across_files(tmp_path):
    make_task(
        tmp_path,
        {
            "az-aks-show.json": aks_show("aks-contoso-prod"),
            "az-aks-list.json": json.dumps(
                [{"name": "aks-contoso-dev", "dnsPrefix": "d",
                  "type": "Microsoft.ContainerService/managedClusters"}]
            ),
        },
    )
    findings = lint_task(task_dir_of(tmp_path / "tasks"))
    clusters = [f for f in findings if f.category == "cluster_name"]
    # One finding per conflict group, naming every disagreeing value.
    assert len(clusters) == 1
    assert clusters[0].value == "aks-contoso-dev, aks-contoso-prod"
    assert clusters[0].files == ("az-aks-list.json", "az-aks-show.json")


def test_region_and_zone_set_conflicts(tmp_path):
    make_task(
        tmp_path,
        {
            "az-aks-show.json": aks_show(
                "aks-prod", location="eastus", agentPoolProfiles=[
                    {"name": "sys"}, {"name": "usr", "zones": ["1", "2", "3"]}
                ]
            ),
            "az-vm-show.json": json.dumps(
                {"name": "node", "location": "westus", "zones": ["1", "2"],
                 "type": "Microsoft.Compute/virtualMachines"}
            ),
        },
    )
    categories = {f.category for f in lint_task(task_dir_of(tmp_path / "tasks"))}
    assert "region" in categories
    assert "zone_set" in categories


def test_multi_resource_list_outputs_do_not_conflict_on_spatial_facts(tmp_path):
    # az-vm-list / az-disk-list style outputs legitimately span zones and
    # locations per entry; only single-resource (dict) documents are compared.
    make_task(
        tmp_path,
        {
            "az-vm-list.json": json.dumps(
                [
                    {"name": "vm-a", "location": "eastus", "zones": ["1"],
                     "type": "Microsoft.Compute/virtualMachines"},
                    {"name": "vm-b", "location": "eastus2", "zones": ["2", "3"],
                     "type": "Microsoft.Compute/virtualMachines"},
                ]
            ),
        },
    )
    findings = lint_task(task_dir_of(tmp_path / "tasks"))
    assert not [f for f in findings if f.category in ("region", "zone_set")]


def test_location_global_is_not_a_region(tmp_path):
    make_task(
        tmp_path,
        {
            "az-aks-show.json": aks_show("aks-prod", location="eastus"),
            "dns-zone.json": json.dumps(
                {"name": "zone", "location": "global",
                 "type": "Microsoft.Network/privateDnsZones"}
            ),
        },
    )
    findings = lint_task(task_dir_of(tmp_path / "tasks"))
    assert not [f for f in findings if f.category == "region"]


def test_subscription_and_tenant_guid_conflicts(tmp_path):
    make_task(
        tmp_path,
        {
            "az-aks-show.json": aks_show("aks-prod"),
            "role-assignment.json": json.dumps(
                {"id": "/subscriptions/00000000-0000-0000-0000-000000000009"
                       "/resourceGroups/rg-x/providers/Microsoft.Authorization/roleAssignments/a",
                 "tenantId": "00000000-0000-0000-0000-000000000099"}
            ),
        },
    )
    categories = {f.category for f in lint_task(task_dir_of(tmp_path / "tasks"))}
    assert "subscription_guid" in categories
    assert "tenant_guid" in categories


def test_vault_name_conflict_across_json_and_text(tmp_path):
    make_task(
        tmp_path,
        {
            "kv-show.json": json.dumps(
                {"properties": {"vaultUri": "https://kv-orders-prod.vault.azure.net/"}}
            ),
            "logs.txt": "kv provider initialized for vault https://kv-orders-dev.vault.azure.net/\n",
        },
    )
    findings = lint_task(task_dir_of(tmp_path / "tasks"))
    vaults = [f for f in findings if f.category == "vault_name"]
    assert len(vaults) == 1
    assert vaults[0].value == "kv-orders-dev, kv-orders-prod"


def test_namespace_conflict_is_entity_keyed(tmp_path):
    make_task(
        tmp_path,
        {
            "kubectl-get-sa.yaml": (
                "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n"
                "  name: orders-api\n  namespace: orders\n"
            ),
            "federated-credential.json": json.dumps(
                {"subject": "system:serviceaccount:default:orders-api"}
            ),
            "monitor-sa.yaml": (
                "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n"
                "  name: monitor\n  namespace: kube-system\n"
            ),
        },
    )
    findings = lint_task(task_dir_of(tmp_path / "tasks"))
    ns = [f for f in findings if f.category == "namespace"]
    assert len(ns) == 1
    assert "orders-api" in ns[0].message
    # The unrelated monitor SA in kube-system is not a conflict.
    assert all("monitor" not in f.value for f in ns)


def test_lintignore_suppresses_conflicts(tmp_path):
    make_task(
        tmp_path,
        {
            "kubectl-get-sa.yaml": (
                "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n"
                "  name: orders-api\n  namespace: orders\n"
            ),
            "federated-credential.json": json.dumps(
                {"subject": "system:serviceaccount:default:orders-api"}
            ),
            ".lintignore": "# intentional: the task narrates a namespace move\nnamespace: default\n",
        },
    )
    task_dir = task_dir_of(tmp_path / "tasks")
    assert lint_task(task_dir) == []
    assert load_ignore(task_dir)["namespace"] == {"default"}


def test_bare_lintignore_value_applies_to_all_categories(tmp_path):
    make_task(
        tmp_path,
        {
            "az-aks-show.json": aks_show("aks-prod", location="eastus"),
            "az-vm-show.json": json.dumps(
                {"name": "node", "location": "westus",
                 "type": "Microsoft.Compute/virtualMachines"}
            ),
            ".lintignore": "westus\n",
        },
    )
    findings = lint_task(task_dir_of(tmp_path / "tasks"))
    assert not [f for f in findings if f.category == "region"]


def test_unparseable_files_still_contribute_via_regex(tmp_path):
    make_task(
        tmp_path,
        {
            "truncated.json": '{"name": "aks-prod", "dnsPrefix": "x", "location": "eastus"',
            "logs.txt": "vault https://kv-a.vault.azure.net/ then https://kv-b.vault.azure.net/\n",
        },
    )
    findings = lint_task(task_dir_of(tmp_path / "tasks"))
    assert [f for f in findings if f.category == "vault_name"]
    assert not [f for f in findings if f.category == "cluster_name"]


def test_commands_yaml_and_lintignore_are_not_evidence(tmp_path):
    make_task(
        tmp_path,
        {
            "az-aks-show.json": aks_show("aks-prod"),
            "_commands.yaml": (
                "commands:\n  - command: kubectl get pods -n other\n    stdout: |\n      NAME\n"
            ),
            ".lintignore": "",
        },
    )
    findings = lint_task(task_dir_of(tmp_path / "tasks"))
    assert findings == []


def test_non_cluster_json_name_is_not_a_cluster(tmp_path):
    make_task(
        tmp_path,
        {
            "blob-show.json": json.dumps({"name": "whatever-blob", "container": "c"}),
            "blob-show-2.json": json.dumps({"name": "another-blob", "container": "c"}),
        },
    )
    findings = lint_task(task_dir_of(tmp_path / "tasks"))
    assert not [f for f in findings if f.category == "cluster_name"]


def test_find_conflicts_is_pure_and_sorted():
    facts = [
        ("region", "westus", "b.txt", None),
        ("region", "eastus", "a.json", None),
        ("namespace", "orders", "sa.yaml", "orders-api"),
        ("namespace", "default", "fed.json", "orders-api"),
    ]

    def as_facts(tuples):
        from bench.fixture_lint import Fact

        return [Fact(*t) for t in tuples]

    findings = find_conflicts(as_facts(facts), {}, "t-1")
    # One finding per conflict group, sorted by (category, value).
    assert [f.category for f in findings] == ["namespace", "region"]
    assert findings[0].value == "orders-api -> default, orders"
    assert findings[1].value == "eastus, westus"
    assert findings[1].files == ("a.json", "b.txt")


def test_lint_task_without_fixtures_dir(tmp_path):
    task_dir = tmp_path / "tasks" / "cat" / "cat-001"
    task_dir.mkdir(parents=True)
    assert lint_task(task_dir) == []


def test_unknown_check_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown check"):
        lint_task(tmp_path, check="spelling")


def test_cli_missing_task_returns_two(tmp_path):
    assert main(["lint-fixtures", "--task", "ghost-999", "--tasks-root", str(tmp_path)]) == 2


def test_cli_reports_findings_and_exit_one(tmp_path):
    make_task(
        tmp_path,
        {
            "az-aks-show.json": aks_show("aks-contoso-prod"),
            "other.json": aks_show("aks-contoso-dev"),
        },
    )
    assert main(["lint-fixtures", "--tasks-root", str(tmp_path / "tasks")]) == 1
