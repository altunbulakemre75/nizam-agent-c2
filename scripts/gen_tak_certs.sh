#!/usr/bin/env bash
# NIZAM CoT mTLS Enrollment — self-signed CA + client cert üretir.
#
# Üretim: FreeTAKServer web UI'dan (/datapackage) resmi cert al.
# Dev: bu script ile hızlı self-signed CA.
#
# Kullanım:
#   bash scripts/gen_tak_certs.sh nizam-client

set -euo pipefail

CLIENT_NAME="${1:-nizam-client}"
OUT_DIR="${2:-infra/certs}"
mkdir -p "$OUT_DIR"

# 1. Root CA (tek sefer)
if [[ ! -f "$OUT_DIR/ca.key" ]]; then
  echo "→ CA oluşturuluyor"
  openssl genrsa -out "$OUT_DIR/ca.key" 4096
  openssl req -x509 -new -nodes -key "$OUT_DIR/ca.key" -sha256 -days 3650 \
    -out "$OUT_DIR/ca.crt" \
    -subj "/C=TR/O=NIZAM/CN=NIZAM-DEV-CA"
fi

# 2. Sunucu sertifikası (FreeTAKServer için)
if [[ ! -f "$OUT_DIR/server.key" ]]; then
  echo "→ Server cert oluşturuluyor"
  openssl genrsa -out "$OUT_DIR/server.key" 2048
  openssl req -new -key "$OUT_DIR/server.key" -out "$OUT_DIR/server.csr" \
    -subj "/C=TR/O=NIZAM/CN=localhost"
  openssl x509 -req -in "$OUT_DIR/server.csr" \
    -CA "$OUT_DIR/ca.crt" -CAkey "$OUT_DIR/ca.key" -CAcreateserial \
    -out "$OUT_DIR/server.crt" -days 365 -sha256
fi

# 3. İstemci sertifikası (pytak için)
echo "→ Client cert oluşturuluyor: $CLIENT_NAME"
openssl genrsa -out "$OUT_DIR/${CLIENT_NAME}.key" 2048
openssl req -new -key "$OUT_DIR/${CLIENT_NAME}.key" -out "$OUT_DIR/${CLIENT_NAME}.csr" \
  -subj "/C=TR/O=NIZAM/CN=${CLIENT_NAME}"
openssl x509 -req -in "$OUT_DIR/${CLIENT_NAME}.csr" \
  -CA "$OUT_DIR/ca.crt" -CAkey "$OUT_DIR/ca.key" -CAcreateserial \
  -out "$OUT_DIR/${CLIENT_NAME}.crt" -days 365 -sha256

# 4. Client PKCS#12 bundle (ATAK tablet import için)
openssl pkcs12 -export \
  -in "$OUT_DIR/${CLIENT_NAME}.crt" \
  -inkey "$OUT_DIR/${CLIENT_NAME}.key" \
  -certfile "$OUT_DIR/ca.crt" \
  -out "$OUT_DIR/${CLIENT_NAME}.p12" \
  -passout pass:atakatak

echo ""
echo "✓ Tamamlandı — $OUT_DIR/"
echo ""
echo "pytak config.ini için:"
echo "  PYTAK_TLS_CLIENT_CERT=$OUT_DIR/${CLIENT_NAME}.crt"
echo "  PYTAK_TLS_CLIENT_KEY=$OUT_DIR/${CLIENT_NAME}.key"
echo "  PYTAK_TLS_CA_CERT=$OUT_DIR/ca.crt"
