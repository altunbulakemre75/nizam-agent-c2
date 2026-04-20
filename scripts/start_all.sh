#!/usr/bin/env bash
# NIZAM full-stack başlatıcı — tüm servisleri paralel ayağa kaldırır.
#
# Kullanım:
#   bash scripts/start_all.sh            # tek webcam
#   bash scripts/start_all.sh --multi    # iki webcam (multi-sensor test)
#   bash scripts/start_all.sh --mock-rf  # RF mock publisher ekle

set -euo pipefail

NATS_URL="${NATS_URL:-nats://localhost:6222}"
MULTI_CAMERA=0
MOCK_RF=0

for arg in "$@"; do
  case $arg in
    --multi) MULTI_CAMERA=1 ;;
    --mock-rf) MOCK_RF=1 ;;
    *) echo "Unknown arg: $arg"; exit 1 ;;
  esac
done

# JWT secret (dev)
export NIZAM_JWT_SECRET="${NIZAM_JWT_SECRET:-dev-secret-min-32-chars-rotate-in-prod}"
export NIZAM_WS_AUTH_DISABLED="${NIZAM_WS_AUTH_DISABLED:-false}"

mkdir -p logs

echo "→ COP server"
python -m uvicorn cop.server:app --port 8100 > logs/cop.log 2>&1 &
echo "  PID=$!"

sleep 2

echo "→ Fusion"
python -m services.fusion.fusion_service --nats "$NATS_URL" > logs/fusion.log 2>&1 &
echo "  PID=$!"

echo "→ Gateway"
python -m services.gateway.track_gateway > logs/gateway.log 2>&1 &
echo "  PID=$!"

echo "→ Camera cam-01 (source=0)"
python -m services.detectors.camera.yolo_service \
    --source 0 --sensor-id cam-01 --nats "$NATS_URL" --metrics-port 8001 \
    > logs/camera-01.log 2>&1 &
echo "  PID=$!"

if [[ $MULTI_CAMERA -eq 1 ]]; then
  echo "→ Camera cam-02 (source=1)"
  python -m services.detectors.camera.yolo_service \
      --source 1 --sensor-id cam-02 --nats "$NATS_URL" --metrics-port 8011 \
      > logs/camera-02.log 2>&1 &
  echo "  PID=$!"
fi

echo "→ COP → NATS bridge"
python -m services.bridge.cop_to_nats > logs/bridge.log 2>&1 &
echo "  PID=$!"

if [[ $MOCK_RF -eq 1 ]]; then
  echo "→ RF mock publisher (3 sahte drone)"
  python -m services.detectors.rf.mock_publisher \
      --sensor-id rf-mock-01 --nats "$NATS_URL" --rate 2.0 --drones 3 \
      > logs/rf-mock.log 2>&1 &
  echo "  PID=$!"
fi

echo ""
echo "✓ Tüm servisler başladı. Loglar: logs/"
echo "  COP:       http://localhost:8100"
echo "  Grafana:   http://localhost:5000"
echo "  Gateway:   http://localhost:8200/health"
echo ""
echo "Durdurmak için: pkill -f 'python -m (services|uvicorn)'"
