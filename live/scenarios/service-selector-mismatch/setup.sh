#!/usr/bin/env bash
# Sets up the service-selector-mismatch live scenario (issue #126):
#   1. ensures a cluster exists (creates a kind cluster unless one is reachable)
#   2. applies the shop workloads and injects the stale-selector fault
#   3. provisions a read-only kubeconfig for the benchmarked agent
#   4. verifies the fault took effect before handing the scenario over
#
# Environment knobs: CLUSTER_NAME, KIND_NODE_IMAGE, AGENT_KUBECONFIG,
# TOKEN_TTL, AGENT_ID (ServiceAccount name).

set -euo pipefail

SCENARIO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER_NAME="${CLUSTER_NAME:-sre-bench-live}"
KIND_NODE_IMAGE="${KIND_NODE_IMAGE:-kindest/node:v1.31.2}"
NAMESPACE="shop"
AGENT_ID="${AGENT_ID:-sre-agent}"
AGENT_KUBECONFIG="${AGENT_KUBECONFIG:-${SCENARIO_DIR}/agent.kubeconfig}"
TOKEN_TTL="${TOKEN_TTL:-4h}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180s}"

log() { printf '[setup] %s\n' "$*"; }
die() { printf '[setup] ERROR: %s\n' "$*" >&2; exit 1; }

for tool in kubectl kind; do
  command -v "${tool}" >/dev/null 2>&1 || die "${tool} is required but not installed"
done

# --- 1. Cluster -------------------------------------------------------------
if kubectl cluster-info >/dev/null 2>&1; then
  log "using existing cluster context: $(kubectl config current-context)"
else
  if ! { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }; then
    die "no reachable cluster and docker is not running — cannot create kind cluster"
  fi
  log "creating kind cluster ${CLUSTER_NAME} (image ${KIND_NODE_IMAGE})"
  kind create cluster --name "${CLUSTER_NAME}" --image "${KIND_NODE_IMAGE}" --wait 120s
fi

# --- 2. Workloads + fault ---------------------------------------------------
# app.yaml is the release (new pod labels + healthy cart); fault.yaml is the
# stale Service selector that the release should have updated.
kubectl apply -f "${SCENARIO_DIR}/app.yaml"
kubectl apply -f "${SCENARIO_DIR}/fault.yaml"

log "waiting for deployments to become ready (${WAIT_TIMEOUT})"
kubectl -n "${NAMESPACE}" rollout status deployment/storefront --timeout="${WAIT_TIMEOUT}"
kubectl -n "${NAMESPACE}" rollout status deployment/cart --timeout="${WAIT_TIMEOUT}"

# --- 3. Read-only agent identity --------------------------------------------
kubectl apply -f "${SCENARIO_DIR}/agent-rbac.yaml"

SERVER="$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.server}')"
CA_DATA="$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
[ -n "${SERVER}" ] || die "could not resolve API server address from current context"
TOKEN="$(kubectl -n "${NAMESPACE}" create token "${AGENT_ID}" --duration="${TOKEN_TTL}")"
[ -n "${TOKEN}" ] || die "could not mint a token for ServiceAccount ${AGENT_ID}"

umask 177
cat > "${AGENT_KUBECONFIG}" <<EOF
# Minted by setup.sh for the benchmarked agent — read-only ServiceAccount token
# (ttl ${TOKEN_TTL}). Never commit this file; remove it with teardown.sh.
apiVersion: v1
kind: Config
clusters:
  - name: ${CLUSTER_NAME}
    cluster:
      server: ${SERVER}
      certificate-authority-data: ${CA_DATA}
contexts:
  - name: sre-agent
    context:
      cluster: ${CLUSTER_NAME}
      user: ${AGENT_ID}
      namespace: ${NAMESPACE}
current-context: sre-agent
users:
  - name: ${AGENT_ID}
    user:
      token: ${TOKEN}
EOF
umask 022

# The read-only boundary is the point of live mode: assert it, both directions.
kubectl --kubeconfig "${AGENT_KUBECONFIG}" -n "${NAMESPACE}" get pods >/dev/null \
  || die "agent kubeconfig cannot read pods"
if kubectl --kubeconfig "${AGENT_KUBECONFIG}" -n "${NAMESPACE}" delete pod --all >/dev/null 2>&1; then
  die "agent identity allowed a mutating verb — RBAC is wrong"
fi
log "agent kubeconfig written (read-only verified): ${AGENT_KUBECONFIG}"

# --- 4. Verify the fault took effect ----------------------------------------
wait_for_endpoint_object() {
  local service="$1"
  local attempt=0
  local max_attempts=30
  until kubectl -n "${NAMESPACE}" get endpoints "${service}" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "${attempt}" -ge "${max_attempts}" ]; then
      return 1
    fi
    sleep 1
  done
  return 0
}

wait_for_endpoint_object storefront || die "endpoints object for storefront never appeared"
wait_for_endpoint_object cart || die "endpoints object for cart never appeared"

STOREFRONT_ENDPOINTS="$(kubectl -n "${NAMESPACE}" get endpoints storefront -o jsonpath='{.subsets[*].addresses[*].ip}')"
CART_ENDPOINTS="$(kubectl -n "${NAMESPACE}" get endpoints cart -o jsonpath='{.subsets[*].addresses[*].ip}')"
[ -z "${STOREFRONT_ENDPOINTS}" ] || die "storefront unexpectedly has endpoints (${STOREFRONT_ENDPOINTS}) — fault not active"
[ -n "${CART_ENDPOINTS}" ] || die "cart has no endpoints — the healthy contrast service is not ready"

READY_PODS="$(kubectl -n "${NAMESPACE}" get pods -l app=storefront --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l | tr -d '[:space:]')"
[ "${READY_PODS}" -ge 1 ] || die "no Running storefront pods — workloads did not come up"

cat <<EOF

[setup] ============================================================
[setup] Scenario ready: service selector mismatch (${NAMESPACE} namespace).
[setup]
[setup] SYMPTOM (what on-call sees — investigation starts here):
[setup]   After this morning's release to the shop namespace, /shop on the
[setup]   web storefront returns 502 from the ingress controller for roughly
[setup]   half of requests. All storefront pods show Running and Ready,
[setup]   there are no crash or restart events, and the HPA is not
[setup]   throttling. The ingress-nginx error log is filling with 502
[setup]   entries for the /shop path while /cart on the same ingress class
[setup]   is healthy. Paged by the checkout-availability alert at 09:20 UTC.
[setup]
[setup] Agent access (read-only, ${TOKEN_TTL} token):
[setup]   export KUBECONFIG=${AGENT_KUBECONFIG}
[setup]   kubectl get pods,svc,endpoints -n ${NAMESPACE}
[setup]
[setup] Grade a diagnosis:  ${SCENARIO_DIR}/check.sh diagnosis --answer answer.json
[setup] Apply a patch:      ${SCENARIO_DIR}/remediate.sh --patch service.yaml
[setup] Verify the cluster: ${SCENARIO_DIR}/check.sh remediation
[setup] Tear down:          ${SCENARIO_DIR}/teardown.sh
[setup] ============================================================
EOF
