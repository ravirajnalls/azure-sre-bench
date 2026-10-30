#!/usr/bin/env bash
# Grades a live-mode run (issue #126) in two halves:
#
#   check.sh diagnosis --answer PATH [--rubric PATH]
#       Grades the agent's diagnosis text against the scenario rubric using
#       bench/grader.py keyword-group semantics (delegates to
#       grade_diagnosis.py). Exit 0 = pass; reasons on stderr on failure.
#
#   check.sh remediation [--namespace shop] [--service storefront] [--timeout 60]
#       Asserts live cluster state after remediate.sh applied a patch: the
#       service has endpoints and its selector matches at least one pod.
#
#   check.sh --self-test
#       Offline: grades the committed sample answers (no cluster needed).

set -euo pipefail

SCENARIO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="shop"
SERVICE="storefront"
TIMEOUT_SECS="60"

fail() { printf '[check] FAIL: %s\n' "$*" >&2; exit 1; }
pass() { printf '[check] PASS: %s\n' "$*"; }

MODE="${1:-}"
case "${MODE}" in
  diagnosis)
    shift
    exec python3 "${SCENARIO_DIR}/grade_diagnosis.py" "$@"
    ;;
  --self-test)
    exec python3 "${SCENARIO_DIR}/grade_diagnosis.py" --self-test
    ;;
  remediation)
    shift || true
    ;;
  "")
    fail "usage: check.sh diagnosis|remediation|--self-test"
    ;;
  *)
    fail "unknown mode: ${MODE} (usage: check.sh diagnosis|remediation|--self-test)"
    ;;
esac

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace) NAMESPACE="${2:?}"; shift 2 ;;
    --service) SERVICE="${2:?}"; shift 2 ;;
    --timeout) TIMEOUT_SECS="${2:?}"; shift 2 ;;
    *) fail "unknown argument: $1" ;;
  esac
done

command -v kubectl >/dev/null 2>&1 || fail "kubectl is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
kubectl cluster-info >/dev/null 2>&1 || fail "no reachable cluster — run setup.sh first"

# Endpoints: poll until the endpoints object carries at least one ready
# address, or the timeout expires (endpoint propagation takes a few seconds).
ELAPSED=0
ENDPOINTS=""
while true; do
  ENDPOINTS="$(kubectl -n "${NAMESPACE}" get endpoints "${SERVICE}" -o json 2>/dev/null \
    | python3 -c '
import json, sys
data = json.load(sys.stdin)
addresses = [addr["ip"]
             for subset in (data.get("subsets") or [])
             for addr in (subset.get("addresses") or [])]
print(" ".join(addresses))' || true)"
  [ -n "${ENDPOINTS}" ] && break
  [ "${ELAPSED}" -lt "${TIMEOUT_SECS}" ] || fail "service ${NAMESPACE}/${SERVICE} still has no endpoints after ${TIMEOUT_SECS}s"
  sleep 2
  ELAPSED=$((ELAPSED + 2))
done
pass "service ${NAMESPACE}/${SERVICE} has endpoints: ${ENDPOINTS}"

# Selector: the applied patch must match at least one Running pod.
SELECTOR="$(kubectl -n "${NAMESPACE}" get service "${SERVICE}" -o json 2>/dev/null \
  | python3 -c '
import json, sys
data = json.load(sys.stdin)
selector = data.get("spec", {}).get("selector") or {}
print(",".join(f"{key}={value}" for key, value in sorted(selector.items())))')"
[ -n "${SELECTOR}" ] || fail "service ${NAMESPACE}/${SERVICE} has an empty selector"

MATCHING_PODS="$(kubectl -n "${NAMESPACE}" get pods -l "${SELECTOR}" \
  --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l | tr -d '[:space:]')"
[ "${MATCHING_PODS}" -ge 1 ] \
  || fail "selector '${SELECTOR}' matches no Running pods"
pass "selector '${SELECTOR}' matches ${MATCHING_PODS} Running pod(s)"
