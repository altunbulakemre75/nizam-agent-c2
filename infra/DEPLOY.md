# NIZAM Deployment Notları

## Şu Anki Durum (Faz 0 MVP)

Lokal geliştirme ve demo için:

```bash
docker compose -f infra/docker-compose.yml up -d
```

Kamal deploy konfigürasyonu **şimdilik kurulmuyor**. Sebep:
- Pilot müşteri / staging çıkana kadar gereksiz karmaşa
- Solo geliştirici için bakım yükü
- Prototip TRL 6 aşamasında

## Pilot Aşamasında Eklenecekler

Pilot müşteri talep ettiğinde bu dosyalar oluşturulacak:

- `infra/kamal/deploy.yml` — SSH hedef VPS + image registry
- `.env.production` — prod secret'leri (Anthropic API, DB şifre, mTLS cert)
- `infra/kamal/hooks/` — pre-deploy DB migration, post-deploy health check

## Önerilen Hedef

Tek-VPS pilot için Hetzner CX41 (€15/ay):
- 4 vCPU + 16GB RAM (Docker + postgres + redpanda için yeterli)
- 160GB NVMe
- Finland/Germany — Avrupa savunma müşterileri için düşük gecikme

## Üretim Öncesi Kontrol Listesi

- [ ] mTLS sertifikaları (pytak enrollment)
- [ ] PostgreSQL backup (pgbackrest + S3-compatible storage)
- [ ] Prometheus retention 90 gün
- [ ] Loki log rotation
- [ ] Grafana admin şifre `.env.production`'dan
- [ ] Firewall: sadece 443 (Grafana/UI), 8089 (CoT TLS), 22 (SSH) açık
- [ ] Secret rotation: 90 günde bir

## Kamal Komutları (Gelecek)

```bash
# İlk deploy:
kamal setup

# Güncellemeler:
kamal deploy

# Rollback:
kamal rollback

# Logs:
kamal app logs -f
```
