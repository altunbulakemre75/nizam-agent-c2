# İnsan-Gereği Görevler (Kod Değil)

NIZAM'ın kod tarafı %95+ kapalı. Pilot müşteriye gitmek için **insan
tarafında** bitirilmesi gereken işler.

## #2 — Saha Testi (Gerçek Drone)

**Hedef:** 1 günde TRL 4 → TRL 5.

**Malzeme:**
- Herhangi bir DJI Mini (~€450 yeni, arkadaştan ödünç $0)
- Laptop + webcam (varsa harici USB kamera)
- Güneşli park/boş arsa (TMMOB izin alanı)
- 2 saat

**Prosedür:**
1. Laptop'ta sistem başlat: `bash scripts/start_all.sh`
2. Drone aç, 20-100m mesafede havalanıp dolaş
3. Ekranda YOLO tespit → NATS → fusion → UI akışını izle
4. **Ekran kaydı + kamera açısı + gerçek drone** aynı kadrajda — bu tek video demo'nun %80'i
5. 5-6 farklı açı/mesafe/ışık — farklı senaryo kanıtları

**Çıktı:** `docs/demo/saha-test-YYYYMMDD/` klasörü, video + screenshot.

## #4 — False Positive Baseline Ölçümü

**Amaç:** "Sistemin FP oranı X/saat" cümlesi kurabilmek.

**Prosedür:**
1. 1 saat video kayıt al (sokak, park, balkon — drone YOK)
2. `python -m services.detectors.camera.yolo_service --source sokak.mp4`
3. `curl localhost:8001/metrics | grep detections_total` — kaç "drone" geçti?
4. Her bir detection için `detect.log` kaydet + görüntü önizleme al
5. Elle etiketle: kuş / uçak / leylek / yansıma / dron / hava balonu
6. Rapor:
   ```
   Süre:        1 saat
   Gerçek drone: 0
   False positive: 12 (kuş 8, uçak 2, yansıma 2)
   FP/saat:      12
   ```

**Hedef:** Drone fine-tune sonrası FP/saat < 2.

## #5 — 72 Saat Kararlılık Testi

**Prosedür:**
1. Laptop'a kur, şarjı takılı bırak, kapağı açık tut
2. `bash scripts/start_all.sh`
3. 72 saat boyunca dokunma
4. Her 12 saatte bir:
   ```bash
   docker stats --no-stream                  # memory/CPU
   df -h                                     # disk
   curl localhost:8001/metrics | grep fps
   docker logs nizam-prometheus 2>&1 | tail -20  # disk doldu mu?
   ls -la logs/ | sort -k5 -n | tail         # log büyümesi
   ```
5. Memory leak varsa: hangi process? `top` ile izle
6. Crash varsa: hangi servis? log'dan trace al

**Kabul kriteri:**
- 72 saat sonunda her servis hâlâ up
- Memory artışı <20% (baseline'a göre)
- Disk: `logs/` < 5GB, Prometheus retention max 30gün
- Crash sayısı: 0 (kabul edilebilir), >0 ise root cause + fix

**Çıktı:** `docs/stability-report-YYYYMMDD.md`

## #17 — 3 Dakikalık Demo Video

**Senaryo:**
1. **0:00-0:30** — Problem anlatımı (stock footage: drone tehdit, havalimanı)
2. **0:30-2:00** — Sistem ekranı (canlı demo, webcam + COP sim)
3. **2:00-3:00** — Teknoloji katmanı (Grafana dashboard + ROE YAML + güvenlik)

**Araçlar:**
- OBS Studio (ücretsiz ekran kaydı)
- Bunu kayıt ederken kendi sesini monte et (Audacity)
- Video kurgusu: DaVinci Resolve (ücretsiz) veya ClipChamp

**Çıktı:** YouTube unlisted link. LinkedIn + mail taslaklarına ekle.

## #18 — 1 Sayfalık Teknik Özet (One-Pager PDF)

**İçerik:**
- Üst 1/3: Sorun + çözüm + hedef müşteri
- Orta 1/3: Mimari diyagram + temel rakamlar (833 test, %95 faz, 8 katman)
- Alt 1/3: İletişim + GitHub link + demo link

**Araç:** Canva (ücretsiz template), 1 saat.

**Çıktı:** `docs/one-pager.pdf` + basılı hali savunma etkinliklerine.

## #20 — İlk Müşteri Görüşmesi Planı

**3 hedef:**

1. **Üniversiteden hoca** (savunma/robotik/AI alanında)
   - Kontak: LinkedIn veya eski ödev hocası
   - Mail: "Tez/danışmanlık için NIZAM gösterebilir miyim?"

2. **LinkedIn'de savunma şirketi çalışanı** (Havelsan/Aselsan R&D)
   - Kontak: LinkedIn mesaj (1. derece bağlantı varsa)
   - Mail: "Counter-UAS prototipim var, 15 dk geri-bildirim alabilir miyim?"

3. **Eski arkadaş** (mühendislik, savunma-adjacent)
   - Kontak: WhatsApp
   - Mail: "Proje açıkladım, kafanda soru varsa"

**Mail şablonu** (`docs/outreach-template.md`):
```markdown
Merhaba [isim],

Son 6 ayda NIZAM isimli açık-kaynak counter-UAS sistemi geliştiriyorum —
Anduril Lattice alternatifi, Türk savunma sanayiine özel.

- 833 test geçiyor
- ATAK-uyumlu (pytak + mTLS)
- Offline çalışır (Ollama yerel LLM)
- Savunma-güvenli karar katmanı (ENGAGE operatör onayı zorunlu)

Demo (3dk): [YouTube link]
GitHub: https://github.com/altunbulakemre75/nizam-cop

15 dakika geri-bildirim alabilir miyim? Çarşamba veya Cuma uygun mudur?

Emre Altunbulak
altunbulakemre75@gmail.com
```

## Öncelik Sırası (Tavsiye)

**Bu hafta:**
1. #17 Demo video kaydı (gerçek drone YOKKEN bile webcam + simülasyon yeter)
2. #18 One-pager PDF
3. #4 FP baseline (1 saat video kayıt + analiz)

**Önümüzdeki hafta:**
4. #1 Drone fine-tune (bkz [drone-finetune.md](drone-finetune.md))
5. #2 Saha testi (DJI bulduğunda)

**3 hafta içinde:**
6. #5 72 saat stress test
7. #20 İlk 3 insan kontağı

Kod paralel devam edebilir — #17 olmadan kimse NIZAM'ı görmüyor.
