# Altyapı (Docker) Runbook

Tüm altyapı: `infra/docker-compose.yml`

## Başlatma

```bash
cd infra
docker compose up -d                    # ana stack
docker compose --profile tak up -d      # FreeTAKServer dahil
```

## Servis Durumu

```bash
docker ps --format "table {{.Names}}\t{{.Status}}"
```

Sağlıklı durumda 11 container:
- nizam-postgres (healthy)
- nizam-nats (healthy)
- nizam-nats-exporter
- nizam-redpanda (healthy) + nizam-redpanda-console
- nizam-prometheus
- nizam-loki + nizam-promtail
- nizam-tempo
- nizam-grafana
- nizam-portainer

## Yeniden Başlatma

```bash
docker restart nizam-<service>          # tek servis
docker compose restart                  # hepsini
docker compose down && docker compose up -d    # temiz restart (volume kaybı YOK)
```

## Volume Sıfırlama (DİKKAT: veri kaybı!)

```bash
docker compose down -v                  # postgres + grafana + tüm data silinir
```

## Port Çakışması

Yeni port haritası (Nisan 2026'da çakışma yaşadıktan sonra değişti):
| Servis | Host port | Container port |
|---|---|---|
| PostgreSQL | 7432 | 5432 |
| NATS client | 6222 | 4222 |
| NATS monitoring | 6223 | 8222 |
| Redpanda kafka | 21092 | 21092 |
| Redpanda admin | 11644 | 9644 |
| Prometheus | 11090 | 9090 |
| Loki | 5100 | 3100 |
| Tempo HTTP | 5200 | 3200 |
| Tempo OTLP | 6317/6318 | 4317/4318 |
| Grafana | 5000 | 3000 |
| Portainer | 11000/11443 | 9000/9443 |

Yeni çakışma olursa `infra/docker-compose.yml` host portunu değiştir.
