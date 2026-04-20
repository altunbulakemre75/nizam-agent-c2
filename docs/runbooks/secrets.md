# Secret Yönetimi Runbook

## Dev vs Prod — Net Ayrım

| Seviye | Ne Kullanılır | Kim Rotate Eder |
|---|---|---|
| **Dev (bu repo)** | `.env.example` + varsayılanlar (`nizam_dev`) | Herkesin elinde |
| **Staging/Pilot** | Docker secrets + `.env.production` (git'te YOK) | DevOps |
| **Prod/Savunma** | HashiCorp Vault / AWS Secrets Manager + auto-rotation | Komutanlık IT |

**Bu repo varsayılanları (`POSTGRES_PASSWORD=nizam_dev`, `GRAFANA_PASSWORD=nizam_dev`)
PRODUCTION'da KULLANILMAZ.** Askeri müşteri ilk soracağı şey bu.

## Secret Envanteri

Deploy öncesi tüm aşağıdaki secret'ler rotate edilmeli:

| Secret | Nerede | Gerçek Değer Kaynağı |
|---|---|---|
| `POSTGRES_PASSWORD` | infra/.env | Vault `kv/nizam/postgres` |
| `GRAFANA_PASSWORD` | infra/.env | Vault `kv/nizam/grafana` |
| `ANTHROPIC_API_KEY` | env (servis başlatırken) | Vault `kv/nizam/anthropic` |
| `NIZAM_JWT_SECRET` | env (gateway başlatırken) | Vault `kv/nizam/jwt` (>=32 karakter) |
| `NIZAM_NATS_PUBLISHER_NKEY` | infra/nats/.env | `scripts/gen_nats_keys.sh` → Vault |
| `NIZAM_NATS_SUBSCRIBER_NKEY` | infra/nats/.env | Aynı |
| TAK mTLS CA | infra/certs/ca.crt | Kurum internal CA |
| TAK mTLS client cert/key | infra/certs/nizam-client.{crt,key} | `scripts/gen_tak_certs.sh` |
| `NIZAM_DB_DSN` | env | Vault, rotation 90 gün |

## Production Setup Adımları

### 1. Vault kurulumu (tek kez)

```bash
# Vault server + auto-unseal (AWS KMS önerilir)
docker run --cap-add=IPC_LOCK -p 8200:8200 \
    -e VAULT_DEV_ROOT_TOKEN_ID=... vault:latest

# Nizam path'leri
vault secrets enable -path=nizam kv-v2
vault kv put nizam/postgres password=$(openssl rand -base64 32)
vault kv put nizam/grafana password=$(openssl rand -base64 32)
vault kv put nizam/anthropic api_key=sk-ant-...
vault kv put nizam/jwt secret=$(openssl rand -base64 48)
```

### 2. Servis başlatırken secret inject

```bash
export POSTGRES_PASSWORD=$(vault kv get -field=password nizam/postgres)
export NIZAM_JWT_SECRET=$(vault kv get -field=secret nizam/jwt)
export ANTHROPIC_API_KEY=$(vault kv get -field=api_key nizam/anthropic)

docker compose up -d
```

### 3. Otomatik Rotation (90 gün)

Vault'un kendi rotation'ı yerine basit cron:

```bash
# /etc/cron.d/nizam-secret-rotation
0 2 1 */3 *  root  /opt/nizam/scripts/rotate_secrets.sh && systemctl restart nizam
```

## Docker Secrets Alternatifi (Vault yoksa)

```yaml
# docker-compose.prod.yml
services:
  postgres:
    environment:
      POSTGRES_PASSWORD_FILE: /run/secrets/postgres_pw
    secrets:
      - postgres_pw

secrets:
  postgres_pw:
    file: /etc/nizam/secrets/postgres_pw.txt
    # mode: 0400, uid/gid: servis kullanıcısı
```

## Şifre Gücü Kontrol Listesi

- [ ] Postgres şifresi: 32 karakter base64, sadece servis hesabı erişir
- [ ] Grafana admin: 2FA + SSO (LDAP/SAML) — şifre tek başına yeterli değil
- [ ] JWT secret: min 48 karakter, 90 günde rotate (mevcut token'lar invalidate)
- [ ] NATS nkey: seed dosyaları `chmod 400`, sadece servis hesabı
- [ ] mTLS CA: offline HSM veya air-gapped machine'de tutulur
- [ ] Anthropic API key: sadece outbound (rate limit + usage alerts)

## Sızıntı Yanıtı

Secret sızdığında:
1. Vault'ta derhal rotate: `vault kv put nizam/<path> ...`
2. Servisleri restart (yeni secret yüklesin)
3. JWT sızdıysa: `NIZAM_JWT_SECRET` değiştir → tüm aktif token'lar invalid
4. NATS nkey sızdıysa: `nats-server.conf`'ten kaldır + yeni key gen
5. Audit log: Kim, ne zaman, hangi IP'den erişmiş — tempo/loki'den çek
