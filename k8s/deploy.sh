#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  NIZAM COP — Kubernetes deployment script
#
#  Usage:
#    ./k8s/deploy.sh          # Apply all manifests
#    ./k8s/deploy.sh delete   # Tear down everything
#    ./k8s/deploy.sh status   # Show pod/svc status
# ─────────────────────────────────────────────────────────────

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

case "${1:-apply}" in

  apply)
    echo "==> Building Docker images..."
    docker build -t nizam-cop:latest -f Dockerfile .
    docker build -t nizam-orchestrator:latest -f Dockerfile.orchestrator .

    echo "==> Applying Kubernetes manifests..."
    kubectl apply -f "$DIR/namespace.yaml"
    kubectl apply -f "$DIR/configmap.yaml"
    kubectl apply -f "$DIR/secret.yaml"
    kubectl apply -f "$DIR/db.yaml"

    echo "==> Waiting for DB..."
    kubectl -n nizam rollout status statefulset/db --timeout=120s

    kubectl apply -f "$DIR/orchestrator.yaml"
    kubectl apply -f "$DIR/cop.yaml"
    kubectl apply -f "$DIR/hpa.yaml"
    kubectl apply -f "$DIR/ingress.yaml"

    echo "==> Waiting for deployments..."
    kubectl -n nizam rollout status deployment/orchestrator --timeout=90s
    kubectl -n nizam rollout status deployment/cop --timeout=90s

    echo "==> Starting pipeline job..."
    kubectl delete job pipeline -n nizam --ignore-not-found
    kubectl apply -f "$DIR/pipeline.yaml"

    echo ""
    echo "==> NIZAM COP deployed!"
    kubectl -n nizam get pods,svc,hpa
    echo ""
    echo "Access: kubectl port-forward svc/cop 8100:8100 -n nizam"
    echo "   or:  http://nizam.local (if ingress configured)"
    ;;

  delete)
    echo "==> Tearing down NIZAM..."
    kubectl delete namespace nizam --ignore-not-found
    echo "Done."
    ;;

  status)
    kubectl -n nizam get pods,svc,hpa,ingress 2>/dev/null || echo "Namespace 'nizam' not found."
    ;;

  *)
    echo "Usage: $0 {apply|delete|status}"
    exit 1
    ;;
esac
