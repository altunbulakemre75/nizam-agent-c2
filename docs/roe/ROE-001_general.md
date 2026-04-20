# ROE-1 — Genel Kurallar

## Amaç
Counter-UAS sisteminin hangi durumlarda hangi eylemleri alabileceğini tanımlar.
Tüm operatörler ve otonom bileşenler bu doktrine uyar.

## Tanımlar
- **Tehdit seviyesi LOW**: bilinen, kayıtlı, korumalı bölge dışındaki drone.
- **Tehdit seviyesi MEDIUM**: bilinmeyen transponder, yavaş hareket, kritik bölge dışı.
- **Tehdit seviyesi HIGH**: kritik bölge çevresinde bilinmeyen drone, agresif hız.
- **Tehdit seviyesi CRITICAL**: korumalı bölgeye doğru agresif yaklaşım.

## Eylemler
- **LOG**: sadece audit trail'e kaydet.
- **ALERT**: operatöre görsel/işitsel uyarı.
- **ENGAGE**: karşı önlem (yalnızca üst komuta onayından sonra).
- **HANDOFF**: başka sistemlere/birliklere devret.

# ROE-2 — Sivil Alanlar

Sivil havalimanı, okul, hastane gibi alanların 5km yarıçap içinde
**ENGAGE asla otomatik tetiklenmez**; sadece HANDOFF veya ALERT.

# ROE-3 — Kimlikli Drone

OpenDroneID ile kayıtlı drone LOG seviyesinde kalır; sadece korumalı
bölgeye girerse MEDIUM'a yükselir.

# ROE-4 — Gece Uçuşu

Gece saatlerinde (22:00-06:00) bilinmeyen drone'lar için LOG yerine
ALERT varsayılanı.
