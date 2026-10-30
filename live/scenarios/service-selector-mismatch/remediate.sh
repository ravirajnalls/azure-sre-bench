#!/usr/bin/env bash
# Grader-side remediation: applies the agent's proposed Service patch.
#
# The benchmarked agent holds a read-only identity (agent-rbac.yaml), so the
# ONLY cluster mutation path is this script — and it refuses anything that is
# not the storefront Service in the shop namespace.
#
# Usage: remediate.sh --patch PATH   (single-document Service manifest)

set -euo pipefail

NAMESPACE="shop"
SERVICE="storefront"

log() { printf '[remediate] %s\n' "$*"; }
die() { printf '[remediate] ERROR: %s\n' "$*" >&2; exit 1; }

PATCH=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --patch) PATCH="${2:?--patch needs a path}"; shift 2 ;;
    *) die "unknown argument: $1 (usage: remediate.sh --patch PATH)" ;;
  esac
done
[[ -n "${PATCH}" ]] || die "--patch PATH is required"
[[ -f "${PATCH}" ]] || die "patch file not found: ${PATCH}"

command -v kubectl >/dev/null 2>&1 || die "kubectl is required"
kubectl cluster-info >/dev/null 2>&1 || die "no reachable cluster — run setup.sh first"

# Decode and validate the patch without a YAML parser (kubectl emits JSON).
NORMALIZED="$(kubectl create --dry-run=client -o json -f "${PATCH}" 2>/dev/null)" \
  || die "patch is not a valid Kubernetes manifest"
KIND="$(printf '%s' "${NORMALIZED}" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("kind", ""))')"
NAME="$(printf '%s' "${NORMALIZED}" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("metadata", {}).get("name", ""))')"
NS="$(printf '%s' "${NORMALIZED}" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("metadata", {}).get("namespace", ""))')"

[[ "${KIND}" == "Service" ]] || die "refusing: patch must be a Service (got ${KIND:-unknown})"
[[ "${NAME}" == "${SERVICE}" ]] || die "refusing: patch must target the ${SERVICE} service (got ${NAME:-unknown})"
[[ "${NS}" == "${NAMESPACE}" ]] || die "refusing: patch must target the ${NAMESPACE} namespace (got ${NS:-unset})"

kubectl apply -f "${PATCH}"
log "patch applied: ${KIND} ${NS}/${NAME}"
