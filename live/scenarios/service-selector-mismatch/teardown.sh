#!/usr/bin/env bash
# Tears the live scenario down (issue #126): deletes the shop namespace and,
# if this scenario created it, the kind cluster. Best-effort by design — every
# step reports and moves on so CI teardown never wedges on a partial state.
#
# Environment knobs: CLUSTER_NAME, AGENT_KUBECONFIG. Delete the cluster only
# when DELETE_CLUSTER=1 (set by CI; local runs usually want to keep it).

set -uo pipefail

SCENARIO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER_NAME="${CLUSTER_NAME:-sre-bench-live}"
NAMESPACE="shop"
AGENT_KUBECONFIG="${AGENT_KUBECONFIG:-${SCENARIO_DIR}/agent.kubeconfig}"

log() { printf '[teardown] %s\n' "$*"; }

if command -v kubectl >/dev/null 2>&1 && kubectl cluster-info >/dev/null 2>&1; then
  if kubectl delete namespace "${NAMESPACE}" --ignore-not-found --timeout=60s; then
    log "namespace ${NAMESPACE} deleted"
  else
    log "WARNING: namespace ${NAMESPACE} could not be deleted"
  fi
else
  log "no reachable cluster — skipping namespace cleanup"
fi

# Remove the agent's credential as soon as the cluster is gone (or unreachable).
if rm -f "${AGENT_KUBECONFIG}" 2>/dev/null; then
  log "agent kubeconfig removed"
else
  log "WARNING: could not remove ${AGENT_KUBECONFIG}"
fi

if [ "${DELETE_CLUSTER:-0}" = "1" ]; then
  if command -v kind >/dev/null 2>&1 && kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
    if kind delete cluster --name "${CLUSTER_NAME}"; then
      log "kind cluster ${CLUSTER_NAME} deleted"
    else
      log "WARNING: kind cluster ${CLUSTER_NAME} could not be deleted"
    fi
  else
    log "no kind cluster named ${CLUSTER_NAME} found — nothing to delete"
  fi
else
  log "DELETE_CLUSTER not set — leaving the kind cluster in place"
fi

exit 0
