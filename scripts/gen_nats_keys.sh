#!/usr/bin/env bash
# NIZAM NATS nkey üretici — publisher + subscriber rolleri için ayrı anahtar.
#
# Gereksinim: nk (https://github.com/nats-io/nkeys — go install ile)
#
# Kullanım:
#   bash scripts/gen_nats_keys.sh
# Çıktı:
#   infra/nats/publisher.nk   — seed (gizli, .gitignore)
#   infra/nats/subscriber.nk  — seed (gizli)
#   infra/nats/.env           — NKEY public'lerini içerir (docker-compose)

set -euo pipefail
OUT_DIR="${1:-infra/nats}"
mkdir -p "$OUT_DIR"

if ! command -v nk &>/dev/null; then
  echo "Hata: nk kurulu değil. Kur: go install github.com/nats-io/nkeys/nk@latest"
  exit 1
fi

echo "→ Publisher nkey üretiliyor..."
nk -gen user > "$OUT_DIR/publisher.nk"
PUB_NKEY=$(nk -inkey "$OUT_DIR/publisher.nk" -pubout)

echo "→ Subscriber nkey üretiliyor..."
nk -gen user > "$OUT_DIR/subscriber.nk"
SUB_NKEY=$(nk -inkey "$OUT_DIR/subscriber.nk" -pubout)

cat > "$OUT_DIR/.env" <<EOF
NIZAM_NATS_PUBLISHER_NKEY=$PUB_NKEY
NIZAM_NATS_SUBSCRIBER_NKEY=$SUB_NKEY
EOF

chmod 600 "$OUT_DIR"/*.nk
echo ""
echo "✓ Tamamlandı — $OUT_DIR/"
echo ""
echo "Publisher public: $PUB_NKEY"
echo "Subscriber public: $SUB_NKEY"
echo ""
echo "Servisler bu seed'leri nats.connect(nkeys_seed='...') ile kullanmalı:"
echo "  import nats"
echo "  nc = await nats.connect('nats://localhost:6222', nkeys_seed='\$OUT_DIR/publisher.nk')"
