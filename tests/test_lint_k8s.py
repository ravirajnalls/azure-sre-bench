"""Unit tests for the k8s fixture checker (issue #150).

Engine-independent tests (lintignore parsing, version resolution, document
classification, List unwrapping, CLI wiring) run against a fake engine so the
suite needs no schema package; the real-engine tests skip unless
`kubernetes-validate` is installed (the CI lint-fixtures job installs it).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bench.lint_fixtures import SKIPPED, WARNING
from bench.lint_k8s import (
    DEFAULT_KUBERNETES_VERSION,
    LINTIGNORE_FILENAME,
    KubernetesValidateEngine,
    ResourceInvalid,
    SchemaUnavailable,
    check_k8s_fixtures,
    kubernetes_version_for_task,
)

REAL_CORPUS = Path(__file__).parent.parent / "tasks"


class FakeEngine:
    """Scriptable engine: kind -> outcome, plus an always-served set."""

    def __init__(
        self,
        invalid_kinds: set[str] | None = None,
        unserved_kinds: set[str] | None = None,
        served_anywhere: set[str] | None = None,
    ):
        self.invalid_kinds = invalid_kinds or set()
        self.unserved_kinds = unserved_kinds or set()
        self.served_anywhere = served_anywhere or set()
        self.validated: list[tuple[str, str]] = []

    def validate(self, document: dict, version: str) -> None:
        kind = document.get("kind", "")
        self.validated.append((str(kind), version))
        if kind in self.invalid_kinds:
            raise ResourceInvalid("spec.replicas must be an integer")
        if kind in self.unserved_kinds:
            raise SchemaUnavailable(f"no schema for {kind}")

    def serves_any_version(self, api_version: str, kind: str) -> bool:
        return kind in self.served_anywhere


def make_task(
    tmp_path: Path,
    files: dict[str, str],
    task_id: str = "monitoring-001",
    az_show: str | None = None,
) -> Path:
    task_dir = tmp_path / "tasks" / "monitoring" / task_id
    fixtures = task_dir / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (fixtures / name).write_text(content, encoding="utf-8")
    (task_dir / "task.yaml").write_text("id: x\n", encoding="utf-8")
    if az_show is not None:
        (fixtures / "az-aks-show.json").write_text(az_show, encoding="utf-8")
    return task_dir


VALID_DEPLOYMENT = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 2
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
      - name: web
        image: nginx:1.27
"""

KUBECTL_GET_WITH_STATUS = """\
apiVersion: v1
kind: Pod
metadata:
  name: web-abc
  managedFields:
  - manager: kubelet
status:
  phase: Running
"""


def check(tmp_path: Path, engine=FakeEngine()):
    """Run the registered k8s check over tmp_path's task tree."""
    return check_k8s_fixtures(tmp_path / "tasks", engine=engine)


# --------------------------------------------------------------------------- #
# lintignore + version resolution
# --------------------------------------------------------------------------- #


def test_lintignore_full_line_comments_and_blanks_are_skipped(tmp_path):
    # Shares #152's fixtures/.lintignore format: bare values (exempt in every
    # category), full-line `#` comments, blank lines. No trailing comments.
    make_task(
        tmp_path,
        {
            "hpa.yaml": "apiVersion: autoscaling/v2beta2\nkind: HorizontalPodAutoscaler\n",
            LINTIGNORE_FILENAME: "# removed API on purpose (issue #150)\n\nhpa.yaml\n",
        },
    )
    assert check(tmp_path) == []


def test_version_from_az_aks_show(tmp_path):
    task = make_task(tmp_path, {}, az_show='{"kubernetesVersion": "1.29.7"}')
    assert kubernetes_version_for_task(task) == "1.29.7"


def test_version_bare_minor_is_padded(tmp_path):
    task = make_task(tmp_path, {}, az_show='{"kubernetesVersion": "1.29"}')
    assert kubernetes_version_for_task(task) == "1.29.0"


def test_version_missing_or_invalid_uses_default(tmp_path):
    task = make_task(tmp_path, {}, az_show='{"kubernetesVersion": "soon"}')
    assert kubernetes_version_for_task(task) == DEFAULT_KUBERNETES_VERSION
    assert kubernetes_version_for_task(tmp_path) == DEFAULT_KUBERNETES_VERSION


# --------------------------------------------------------------------------- #
# Document classification (engine-independent)
# --------------------------------------------------------------------------- #


def test_valid_manifest_and_get_output_are_clean(tmp_path):
    make_task(
        tmp_path,
        {"deployment.yaml": VALID_DEPLOYMENT, "kubectl-get-pod.yaml": KUBECTL_GET_WITH_STATUS},
    )
    assert check(tmp_path) == []


def test_app_config_yaml_without_api_version_is_skipped(tmp_path):
    make_task(tmp_path, {"app-config.yaml": "client:\n  timeout: 10s\n"})
    assert check(tmp_path) == []


def test_partial_resource_is_reported(tmp_path):
    make_task(
        tmp_path,
        {"broken.yaml": "apiVersion: apps/v1\nmetadata:\n  name: web\n"},
    )
    findings = check(tmp_path)
    assert len(findings) == 1
    assert findings[0].check == "k8s"
    assert findings[0].severity == WARNING
    assert "missing kind" in findings[0].message


def test_non_mapping_document_is_reported(tmp_path):
    make_task(tmp_path, {"list.yaml": "- one\n- two\n"})
    findings = check(tmp_path)
    assert len(findings) == 1
    assert "must be a mapping" in findings[0].message


def test_unparseable_yaml_is_reported(tmp_path):
    make_task(tmp_path, {"bad.yaml": "kind: [unclosed\n"})
    findings = check(tmp_path)
    assert len(findings) == 1
    assert "unparseable YAML" in findings[0].message


def test_lintignore_file_is_not_a_fixture_and_exempts_its_entries(tmp_path):
    make_task(
        tmp_path,
        {
            "hpa.yaml": "apiVersion: autoscaling/v2beta2\nkind: HorizontalPodAutoscaler\n",
            "deployment.yaml": VALID_DEPLOYMENT,
            LINTIGNORE_FILENAME: "hpa.yaml\n",
        },
    )
    findings = check(tmp_path)
    assert findings == []


# --------------------------------------------------------------------------- #
# Engine outcome mapping
# --------------------------------------------------------------------------- #


def test_schema_violation_becomes_warning_finding(tmp_path):
    make_task(tmp_path, {"deployment.yaml": VALID_DEPLOYMENT})
    engine = FakeEngine(invalid_kinds={"Deployment"})
    findings = check(tmp_path, engine)
    assert len(findings) == 1
    assert findings[0].severity == WARNING
    assert "invalid apps/v1 Deployment" in findings[0].message
    assert "spec.replicas must be an integer" in findings[0].message


def test_removed_api_reported_when_schema_served_elsewhere(tmp_path):
    make_task(
        tmp_path,
        {"hpa.yaml": "apiVersion: autoscaling/v2beta2\nkind: HorizontalPodAutoscaler\n"},
        az_show='{"kubernetesVersion": "1.29.7"}',
    )
    engine = FakeEngine(
        unserved_kinds={"HorizontalPodAutoscaler"},
        served_anywhere={"HorizontalPodAutoscaler"},
    )
    findings = check(tmp_path, engine)
    assert len(findings) == 1
    assert "1.29.7" in findings[0].message
    assert "removed" in findings[0].message
    assert ".lintignore" in findings[0].message


def test_custom_resource_without_any_schema_is_skipped(tmp_path):
    make_task(
        tmp_path,
        {"crd.yaml": (
            "apiVersion: cert-manager.io/v1\nkind: ClusterIssuer\n"
            "metadata:\n  name: letsencrypt\n"
        )},
    )
    engine = FakeEngine(unserved_kinds={"ClusterIssuer"})  # never served anywhere
    findings = check(tmp_path, engine)
    assert len(findings) == 1
    assert findings[0].severity == SKIPPED


def test_unknown_kind_in_builtin_group_is_reported(tmp_path):
    # policy/v1beta1 vanished before the packaged schema sets start; a miss in
    # a builtin group must still be reported, not treated as a CRD.
    make_task(
        tmp_path,
        {"pdb.yaml": "apiVersion: policy/v1beta1\nkind: PodDisruptionBudget\n"},
        az_show='{"kubernetesVersion": "1.29.7"}',
    )
    engine = FakeEngine(unserved_kinds={"PodDisruptionBudget"})
    findings = check(tmp_path, engine)
    assert len(findings) == 1
    assert findings[0].severity == WARNING
    assert "builtin group 'policy'" in findings[0].message


def test_v1_list_is_unwrapped_and_items_validated(tmp_path):
    make_task(
        tmp_path,
        {"get-many.yaml": (
            "apiVersion: v1\n"
            "kind: List\n"
            "items:\n"
            "- apiVersion: v1\n"
            "  kind: Pod\n"
            "  metadata:\n"
            "    name: ok\n"
            "- apiVersion: v1\n"
            "  kind: Pod\n"
            "  metadata:\n"
            "    name: broken\n"
        )},
    )
    engine = FakeEngine(invalid_kinds={"Pod"})
    findings = check(tmp_path, engine)
    # Both items were unwrapped and validated individually; the engine flags
    # every Pod, so two findings, each pointing at the List document's line.
    assert len(findings) == 2
    assert all(f.path.endswith("get-many.yaml:1") for f in findings)


def test_list_items_must_be_objects(tmp_path):
    make_task(
        tmp_path,
        {"get-many.yaml": "apiVersion: v1\nkind: List\nitems:\n- just a string\n"},
    )
    findings = check(tmp_path)
    assert len(findings) == 1
    assert "items[0]" in findings[0].message


# --------------------------------------------------------------------------- #
# Real engine (skipped without the kubernetes-validate package)
# --------------------------------------------------------------------------- #

pytest.importorskip("kubernetes_validate")


@pytest.fixture
def real_engine():
    return KubernetesValidateEngine()


def test_real_engine_accepts_valid_manifest(tmp_path, real_engine):
    make_task(tmp_path, {"deployment.yaml": VALID_DEPLOYMENT})
    assert check(tmp_path, real_engine) == []


def test_real_engine_flags_type_error(tmp_path, real_engine):
    make_task(
        tmp_path,
        {"deployment.yaml": VALID_DEPLOYMENT.replace("replicas: 2", "replicas: two")},
    )
    findings = check(tmp_path, real_engine)
    assert len(findings) == 1
    assert findings[0].severity == WARNING
    assert "invalid apps/v1 Deployment" in findings[0].message


def test_real_engine_flags_removed_api(tmp_path, real_engine):
    make_task(
        tmp_path,
        {"hpa.yaml": "apiVersion: autoscaling/v2beta2\nkind: HorizontalPodAutoscaler\n"},
        az_show='{"kubernetesVersion": "1.29.7"}',
    )
    findings = check(tmp_path, real_engine)
    assert len(findings) == 1
    assert "1.29.7" in findings[0].message


def test_real_engine_skips_custom_resources(tmp_path, real_engine):
    make_task(
        tmp_path,
        {"spc.yaml": (
            "apiVersion: secrets-store.csi.x-k8s.io/v1\nkind: SecretProviderClass\n"
            "metadata:\n  name: azure-kv\n"
        )},
    )
    findings = check(tmp_path, real_engine)
    assert len(findings) == 1
    assert findings[0].severity == SKIPPED


def test_real_engine_corpus_reports_only_allowlisted_removed_apis(real_engine):
    """Corpus-wide: with the aks-ops-019 allowlist applied, zero violations."""
    findings = check_k8s_fixtures(REAL_CORPUS, engine=real_engine)
    assert [f for f in findings if f.severity != SKIPPED] == [], "\n".join(
        f.message for f in findings if f.severity != SKIPPED
    )


# --------------------------------------------------------------------------- #
# Registry + CLI wiring
# --------------------------------------------------------------------------- #


def test_k8s_check_is_registered():
    from bench.lint_fixtures import CHECKS

    assert CHECKS["k8s"] is check_k8s_fixtures


def run_cli(*argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bench", "lint-fixtures", *argv],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def test_cli_reports_violations_as_warnings_exit_zero(tmp_path):
    make_task(
        tmp_path,
        {"hpa.yaml": "apiVersion: autoscaling/v2beta2\nkind: HorizontalPodAutoscaler\n"},
        az_show='{"kubernetesVersion": "1.29.7"}',
    )
    proc = run_cli("--check", "k8s", "--tasks-root", str(tmp_path / "tasks"))
    assert proc.returncode == 0  # warning-severity findings never fail the run
    assert "WARNING monitoring-001/fixtures/hpa.yaml:1" in proc.stdout
    assert "k8s:" in proc.stdout


def test_cli_unknown_check_name_is_rejected(tmp_path):
    proc = run_cli("--check", "nope", "--tasks-root", str(tmp_path / "tasks"))
    assert proc.returncode == 2
