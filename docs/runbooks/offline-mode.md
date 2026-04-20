# Offline Mode — Air-Gapped Deployment

Savunma sahalarında internet yok/kısıtlı. NIZAM offline çalışabilecek
şekilde tasarlandı. Bu belge hangi bileşenlerin internete ihtiyaç
duyduğunu ve nasıl offline yapılacağını özetler.

## İhtiyaç Tablosu

| Bileşen | Online gerekli? | Offline alternatif |
|---|---|---|
| Python paketleri | İlk kurulum | `pip download -r requirements.txt` + offline pip install |
| YOLO model | İlk kurulum | `yolov8n.pt` pre-download, repo'da sakla |
| Cesium Ion token | Hayır | Boş token → OSM base layer |
| OpenStreetMap tiles | Evet | OpenMapTiles docker + Türkiye tile set |
| Claude API | Evet | **Ollama localhost** (llama3.1:8b) |
| Anthropic metrics | Evet | Sadece yerel Prometheus |
| Docker Hub imageleri | İlk kurulum | `docker save`/`docker load` tar bundle |
| ROE RAG LlamaIndex | Hayır | PDF/Markdown yerel, embedding model yerel |
| NTP zaman senkronu | Evet | Yerel NTP server (GPS disciplined) |

## Ollama Kurulumu (LLM advisor için)

```bash
# 1. Ollama kur (https://ollama.com/download)
# Windows:
winget install Ollama.Ollama
# Linux:
curl -fsSL https://ollama.com/install.sh | sh

# 2. llama3.1:8b indir (~5GB, tek seferlik — online gerekli)
ollama pull llama3.1:8b

# 3. Servis başlat (port 11434)
ollama serve &

# 4. NIZAM env ayarla
export NIZAM_DECISION_LLM_ENABLED=true
export OLLAMA_URL=http://localhost:11434
export OLLAMA_MODEL=llama3.1:8b
# ANTHROPIC_API_KEY SET ETME → Ollama fallback'e düşer
```

## Offline Test Prosedürü

1. Internet kapat (airplane mode):
   ```bash
   # Windows:
   # Wi-Fi off, Ethernet disconnect
   # Linux:
   sudo ip link set wlan0 down
   sudo ip link set eth0 down
   ```

2. Tüm servisleri başlat:
   ```bash
   bash scripts/start_all.sh
   ```

3. Kontroller:
   - [ ] Fusion metrics artıyor mu? `curl localhost:8003/metrics`
   - [ ] LLM advisor devrede mi (Ollama)? Log'da `llm_provider=ollama`
   - [ ] Grafana dashboard erişilebilir mi? http://localhost:5000
   - [ ] Cesium UI açılıyor mu? http://localhost:5173 (Ion token boş, OSM düşer)
   - [ ] Track'ler operatör panelinde mi?
   - [ ] PostgreSQL checkpoint yazılıyor mu?
     ```sql
     psql -U nizam -d nizam -c "SELECT COUNT(*) FROM decisions;"
     ```

4. Karşılaştırma:
   ```bash
   # Online (Claude)
   export ANTHROPIC_API_KEY=sk-ant-...
   # decision latency, reasoning kalitesi

   # Offline (Ollama)
   unset ANTHROPIC_API_KEY
   # aynı decision latency, benzer karar tutarlılığı?
   ```

## Air-Gap Docker Bundle

Bir kez internetli makine hazırla:

```bash
# Tüm image'ları çek
docker compose -f infra/docker-compose.yml pull

# Tar bundle olarak kaydet
docker save $(docker compose config --images) | gzip > nizam-images.tar.gz

# USB'ye al, saha makinesine götür
docker load < nizam-images.tar.gz

# Sonra normal başlat
docker compose up -d
```

## Veri Dışa Çıkışı (Outbound Audit)

Offline moddan sızıntı olmasın — firewall rule:

```bash
# iptables — sadece local ağa izin, dışarı yasak
iptables -A OUTPUT -d 10.0.0.0/8 -j ACCEPT         # iç ağ
iptables -A OUTPUT -d 192.168.0.0/16 -j ACCEPT
iptables -A OUTPUT -d 127.0.0.0/8 -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -P OUTPUT DROP                             # geri kalan yasak
```

## Şüpheli Outbound İzleme

Prometheus alert ekle:

```yaml
- alert: NizamOutboundConnection
  expr: rate(node_network_transmit_bytes_total{device!="lo"}[1m]) > 1000
  for: 2m
  annotations:
    summary: "Sistem dışarı veri gönderiyor — {{ $labels.device }}"
```

## Tipik Offline Senaryo Sorunları

| Sorun | Çözüm |
|---|---|
| Ultralytics ilk çalıştırmada model indirmeye çalışıyor | `ULTRALYTICS_OFFLINE=1` + `yolov8n.pt` manuel indir |
| Cesium terrain yüklenmiyor | Ion token boş bırak, sadece OSM |
| pytak sunucu sertifikası CA chain eksik | Kendi CA + `gen_tak_certs.sh` |
| Grafana plugin repo çekmeye çalışıyor | `GF_INSTALL_PLUGINS=""` + offline plugin bundle |
| Docker compose `pull` yapmaya çalışıyor | `docker compose up -d --no-pull` (image yerel) |

## "Veriler ABD'ye Gidiyor mu?" Sorusuna Cevap

**Kanıt 1** — Anthropic API bypass edildiğinde:
```bash
# Ollama çalışıyor, Claude yok
curl http://localhost:11434/api/tags   # llama3.1:8b listeli
echo $ANTHROPIC_API_KEY                # boş
```

**Kanıt 2** — decision audit trail:
```sql
SELECT llm_provider, COUNT(*) FROM decisions GROUP BY llm_provider;
-- ollama: 1234
-- anthropic: 0
-- NULL: 56 (LLM kapalı idi)
```

**Kanıt 3** — network ACL:
```bash
iptables -L OUTPUT -n  # DROP policy + sadece iç ağ ACCEPT
```
