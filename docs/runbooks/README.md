# NIZAM Runbook'ları

Her servisin çöküp kalkma prosedürü, alarm yanıtları.

## Servisler

- [cop-server.md](cop-server.md) — Eski COP FastAPI (port 8100)
- [fusion.md](fusion.md) — Füzyon servisi (port 8003)
- [camera.md](camera.md) — YOLO kamera servisi (port 8001)
- [gateway.md](gateway.md) — Track gateway (port 8200)
- [infra.md](infra.md) — Docker altyapı (postgres/nats/redpanda/grafana...)

## Genel Komutlar

```bash
# Tüm servis durumu
docker ps --format "table {{.Names}}\t{{.Status}}"
curl http://localhost:11090/api/v1/targets | jq '.data.activeTargets[] | {job: .labels.job, health}'

# Log görüntüle
docker logs -f nizam-fusion 2>&1 | head -100
# veya Loki'den:
open http://localhost:5000/explore  # datasource: Loki

# Servis yeniden başlat
docker restart nizam-<service>
```

## Alarm Yanıtları

Prometheus alert geldiğinde:
1. **NizamCameraFpsLow** → bkz [camera.md](camera.md)
2. **NizamFusionTickSlow** → bkz [fusion.md](fusion.md)
3. **NizamNoDetectionsAnywhere** → tüm sensörleri kontrol
