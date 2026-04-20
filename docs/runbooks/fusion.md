# Fusion Service Runbook

Port: 8003 | Prometheus: `nizam-fusion` | Metrics: `nizam_fusion_*`

## Normal Başlatma

```bash
python -m services.fusion.fusion_service --nats nats://localhost:6222
```

## Sağlık Kontrolü

```bash
curl http://localhost:8003/metrics | grep nizam_fusion_active_tracks
# Beklenen: pozitif tamsayı (sensör verisi varsa > 0)
```

## Alarmlar

### NizamFusionTickSlow (p95 > 100ms)
**Olası sebepler:**
1. Çok fazla track (>1000) — IMM filtre hesabı ağır
2. Ölçüm kuyruğu dolmuş — queue boyutu 10k limitinde

**Tanı:**
```bash
curl http://localhost:8003/metrics | grep nizam_fusion_active
```

**Aksiyon:**
- Kısa vadeli: servisi restart et
- Orta vadeli: `track_manager`'da lifecycle eşiklerini (M_LOST, K_DELETE) düşür
- Uzun vadeli: müdahale partition — coğrafi olarak fusion instance'ları böl

### NizamFusionNoTracks (ölçüm var ama track yok)
**Olası sebep:** Association gate çok dar veya ölçüm gürültüsü çok yüksek.

**Tanı:**
```bash
curl http://localhost:8003/metrics | grep nizam_fusion_measurements_total
# ölçüm sayısı artıyor mu?
```

**Aksiyon:**
- `services/fusion/association.py` `DEFAULT_GATE` değerini artır (3.77 → 5.0)
- Sensör `sigma_x/y/z` değerlerini büyüt (track manager spawn'da etkin)

## Çöktüğünde

Fusion servisi crash ettiyse:
1. Son log'a bak: `docker logs nizam-fusion 2>&1 | tail -50` (Docker'da koşuyorsa)
2. Port hâlâ binded mi: `netstat -ano | grep 8003`
3. Yeniden başlat: `python -m services.fusion.fusion_service`
4. Health check: `curl http://localhost:8003/metrics`

Fusion çökse bile pipeline'ın geri kalanı etkilenmez — detectors NATS'e
yazmaya devam eder, Gateway eski snapshot'u tutar. Sadece yeni track
üretimi durur.
