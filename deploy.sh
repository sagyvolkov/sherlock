#!/usr/bin/env bash
set -euo pipefail
IMAGE="quay.io/sagyvolkov/sherlock-operator:v2.0.0-alpha1"
NAMESPACE="sherlock"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPERATOR_DIR="${SCRIPT_DIR}/operator"
info()    { echo "[deploy] $*"; }
success() { echo "[deploy] $*"; }
error()   { echo "[deploy] ERROR: $*" >&2; exit 1; }
step()    { echo -e "\n── $* "; }
do_build() {
    step "Building operator image: ${IMAGE}"
    cd "${OPERATOR_DIR}"
    podman build -t "${IMAGE}" .
    podman push "${IMAGE}"
    success "Push complete: ${IMAGE}"
}
do_deploy() {
    step "Creating namespace"
    oc create namespace "${NAMESPACE}" --dry-run=client -o yaml | oc apply -f -
    step "Applying OCP SCC"
    oc apply -f "${OPERATOR_DIR}/deploy/ocp-scc-patch.yaml"
    step "Applying CRDs"
    oc apply -f "${OPERATOR_DIR}/crds/benchmarks/"
    step "Applying RBAC"
    oc apply -f "${OPERATOR_DIR}/rbac/operator-rbac.yaml"
    step "Deploying operator"
    oc apply -f "${OPERATOR_DIR}/deploy/operator-deployment.yaml"
    step "Waiting for operator pod"
    oc rollout status deployment/sherlock-operator -n "${NAMESPACE}" --timeout=120s
    oc get pods -n "${NAMESPACE}" -l app=sherlock-operator
    success "Deploy complete!"
}
do_undeploy() {
    oc delete -f "${OPERATOR_DIR}/deploy/operator-deployment.yaml" --ignore-not-found
    oc delete -f "${OPERATOR_DIR}/rbac/operator-rbac.yaml" --ignore-not-found
    oc delete -f "${OPERATOR_DIR}/deploy/ocp-scc-patch.yaml" --ignore-not-found
    [[ "${1:-}" == "crds" ]] && oc delete -f "${OPERATOR_DIR}/crds/benchmarks/" --ignore-not-found
    success "Undeployed"
}
cmd="${1:-help}"
case "$cmd" in
    build)        do_build ;;
    deploy)       do_deploy ;;
    all)          do_build && do_deploy ;;
    undeploy)     do_undeploy ;;
    undeploy-all) do_undeploy crds ;;
    logs)         oc logs -n "${NAMESPACE}" -l app=sherlock-operator --follow ;;
    *)            echo "Usage: ./deploy.sh build|deploy|all|undeploy|undeploy-all|logs" ;;
esac
