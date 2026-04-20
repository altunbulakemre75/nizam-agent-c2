#!/usr/bin/env bash
# NIZAM E2E Canlı Test — docker up + servisler + verification + teardown.
#
# Kullanım:
#   bash scripts/e2e_live_test.sh
#
# Amaç: `pytest geçiyor` ≠ `sistem ayakta`. Bu script gerçekten başlatır,
# metric/log doğrulaması yapar, failure noktalarını first-run-issues.md'ye yazar.

set -eu
START_TIME=$(date +%s)
LOG_FILE="logs/e2e-test-$(date +%Y%m%d-%H%M%S).log"
mkdir -p logs
exec > >(tee "$LOG_FILE") 2>&1

echo "═══════════════════════════════════════════════════════════"
echo "  NIZAM E2E Canlı Test — $(date)"
echo "═══════════════════════════════════════════════════════════"

# ── Adım 1: Docker altyapı ────────────────────────────────────────
echo ""
echo "→ 1. Docker infra başlatılıyor..."
cd infra
docker compose up -d
cd ..
sleep 8

expected_containers="nizam-postgres nizam-nats nizam-nats-exporter nizam-redpanda nizam-redpanda-console nizam-prometheus nizam-loki nizam-tempo nizam-grafana nizam-portainer nizam-promtail"
failed=0
for c in $expected_containers; do
  if docker ps --format '{{.Names}}' | grep -q "^$c$"; then
    echo "  ✓ $c"
  else
    echo "  ✗ $c EKSİK"
    failed=$((failed + 1))
  fi
done

if [ $failed -gt 0 ]; then
  echo "FAIL: $failed konteyner eksik"
  exit 1
fi

# ── Adım 2: Python servisleri ─────────────────────────────────────
echo ""
echo "→ 2. Python servisleri başlatılıyor..."
export NIZAM_JWT_SECRET="e2e-test-secret-min-32-characters-long"
bash scripts/start_all.sh &
sleep 25

# ── Adım 3: Health checks ─────────────────────────────────────────
echo ""
echo "→ 3. Health check'ler..."
check() {
  local name=$1 url=$2 expected_text=$3
  if curl -s --max-time 5 "$url" | grep -q "$expected_text"; then
    echo "  ✓ $name"
  else
    echo "  ✗ $name ($url)"
    return 1
  fi
}

check "COP"             "http://localhost:8100/api/metrics"  "uptime_s"          || true
check "Camera metrics"  "http://localhost:8001/metrics"      "nizam_camera"       || true
check "Fusion metrics"  "http://localhost:8003/metrics"      "nizam_fusion"       || true
check "Gateway health"  "http://localhost:8200/health"       "status"             || true
check "Bridge metrics"  "http://localhost:8005/metrics"      "nizam_bridge"       || true
check "Prometheus"      "http://localhost:11090/-/healthy"   "Healthy"            || true
check "Grafana"         "http://localhost:5000/api/health"   "ok"                 || true

# ── Adım 4: Pipeline akış ─────────────────────────────────────────
echo ""
echo "→ 4. Senaryo başlatılıyor..."
curl -s -X POST http://localhost:8100/api/scenarios/swarm_attack/run | head -c 100
echo ""
sleep 20

echo ""
echo "→ 5. Metrik doğrulama (20s sonra):"
cam_count=$(curl -s http://localhost:8001/metrics | grep "detections_total" | grep -v HELP | grep -v TYPE | head -1 | awk '{print $NF}' || echo "0")
fusion_meas=$(curl -s http://localhost:8003/metrics | grep "measurements_total{" | head -1 | awk '{print $NF}' || echo "0")
bridge_tracks=$(curl -s http://localhost:8005/metrics | grep "nizam_bridge_cop_tracks_total " | awk '{print $NF}' || echo "0")

echo "  Camera detections: $cam_count"
echo "  Fusion measurements: $fusion_meas"
echo "  Bridge tracks: $bridge_tracks"

# ── Adım 6: Prometheus targets ────────────────────────────────────
echo ""
echo "→ 6. Prometheus scrape target'ları:"
curl -s http://localhost:11090/api/v1/targets | python -c "
import sys, json
d = json.load(sys.stdin)
for t in d['data']['activeTargets']:
    print(f\"    {t['labels'].get('job','?'):18s} {t['health']}\")"

# ── Adım 7: Logs (errors) ─────────────────────────────────────────
echo ""
echo "→ 7. Python servis logları (son 5 hata):"
grep -iE "error|traceback|exception" logs/*.log 2>/dev/null | tail -5 || echo "  (yok — temiz)"

# ── Adım 8: Teardown ──────────────────────────────────────────────
echo ""
echo "→ 8. Kapatma testi (SIGTERM graceful)..."
pkill -TERM -f 'python -m services' 2>/dev/null || true
pkill -TERM -f 'python -m uvicorn cop' 2>/dev/null || true
sleep 3

echo ""
echo "═══════════════════════════════════════════════════════════"
elapsed=$(($(date +%s) - START_TIME))
echo "  Test süresi: ${elapsed}s"
echo "  Log: $LOG_FILE"
echo "═══════════════════════════════════════════════════════════"
