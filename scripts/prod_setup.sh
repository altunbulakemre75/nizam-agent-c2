#!/usr/bin/env bash
# =============================================================================
# scripts/prod_setup.sh — NIZAM COP production environment setup
#
# What this script does:
#   1. Checks for required dependencies (Docker, openssl, python3)
#   2. Creates .env from .env.prod.example with generated secrets
#   3. Generates a self-signed TLS certificate in nginx/certs/
#   4. Optionally starts the full production stack
#
# Usage:
#   chmod +x scripts/prod_setup.sh
#   ./scripts/prod_setup.sh                # setup only
#   ./scripts/prod_setup.sh --start        # setup + start stack
#   ./scripts/prod_setup.sh --start --tls-cn my.domain.com
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ── Colour helpers ───────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── Argument parsing ─────────────────────────────────────────────────────────
START_STACK=false
TLS_CN="nizam-cop"
FORCE=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --start)   START_STACK=true; shift ;;
    --tls-cn)  TLS_CN="$2"; shift 2 ;;
    --force)   FORCE=true; shift ;;
    -h|--help)
      echo "Usage: $0 [--start] [--tls-cn HOSTNAME] [--force]"
      echo "  --start        Start docker-compose.prod.yml after setup"
      echo "  --tls-cn HOST  Common Name for the TLS certificate (default: nizam-cop)"
      echo "  --force        Overwrite existing .env and TLS certs"
      exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

echo -e "\n${BOLD}═══════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  NIZAM COP — Production Setup${NC}"
echo -e "${BOLD}═══════════════════════════════════════════════════${NC}\n"

# ── 1. Dependency checks ─────────────────────────────────────────────────────
info "Checking dependencies…"

command -v python3 >/dev/null 2>&1 || die "python3 not found. Install Python 3.10+"
command -v openssl >/dev/null 2>&1 || die "openssl not found. Install openssl."
command -v docker  >/dev/null 2>&1 || { warn "docker not found — stack cannot be started"; START_STACK=false; }

PYTHON=$(command -v python3)
OPENSSL=$(command -v openssl)

PY_VER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
SSL_VER=$($OPENSSL version | awk '{print $2}')
info "python3 ${PY_VER}, openssl ${SSL_VER}"

if command -v docker &>/dev/null; then
  DOCKER_VER=$(docker --version | awk '{print $3}' | tr -d ',')
  ok "docker ${DOCKER_VER}"
fi

# ── 2. Generate .env ─────────────────────────────────────────────────────────
ENV_FILE="$REPO_ROOT/.env"
ENV_EXAMPLE="$REPO_ROOT/.env.prod.example"

if [[ -f "$ENV_FILE" && "$FORCE" == false ]]; then
  warn ".env already exists — skipping generation (use --force to overwrite)"
else
  info "Generating .env from .env.prod.example…"

  [[ -f "$ENV_EXAMPLE" ]] || die ".env.prod.example not found at $ENV_EXAMPLE"

  # Generate secrets
  JWT_SECRET=$($PYTHON -c "import secrets; print(secrets.token_hex(32))")
  DB_PASS=$($PYTHON -c "import secrets; print(secrets.token_urlsafe(20))")
  INGEST_KEY=$($PYTHON -c "import secrets; print(secrets.token_urlsafe(32))")
  NODE_ID="cop-node-$(hostname | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-' | head -c 16)"

  # Substitute placeholders
  sed \
    -e "s|CHANGE_ME_REPLACE_WITH_32_RANDOM_CHARS|${JWT_SECRET}|g" \
    -e "s|CHANGE_ME_STRONG_DB_PASSWORD|${DB_PASS}|g" \
    -e "s|CHANGE_ME_SENSOR_API_KEY|${INGEST_KEY}|g" \
    -e "s|cop-node-01|${NODE_ID}|g" \
    -e "s|^DATABASE_URL=.*|DATABASE_URL=postgresql+asyncpg://nizam:${DB_PASS}@db:5432/nizam|g" \
    "$ENV_EXAMPLE" > "$ENV_FILE"

  ok ".env created at $ENV_FILE"
  echo -e "    JWT_SECRET   : ${JWT_SECRET:0:8}…  (${#JWT_SECRET} chars)"
  echo -e "    DB_PASSWORD  : ${DB_PASS:0:4}…     (${#DB_PASS} chars)"
  echo -e "    INGEST_KEY   : ${INGEST_KEY:0:8}…  (${#INGEST_KEY} chars)"
  echo -e "    NODE_ID      : ${NODE_ID}"
fi

# ── 3. TLS certificate ───────────────────────────────────────────────────────
CERT_DIR="$REPO_ROOT/nginx/certs"
CERT_FILE="$CERT_DIR/nizam.crt"
KEY_FILE="$CERT_DIR/nizam.key"

mkdir -p "$CERT_DIR"

if [[ -f "$CERT_FILE" && -f "$KEY_FILE" && "$FORCE" == false ]]; then
  warn "TLS certs already exist — skipping (use --force to regenerate)"
  CERT_EXPIRY=$($OPENSSL x509 -enddate -noout -in "$CERT_FILE" 2>/dev/null | cut -d= -f2 || echo "unknown")
  info "  Existing cert expires: $CERT_EXPIRY"
else
  info "Generating self-signed TLS certificate (CN=${TLS_CN}, 10 years)…"
  $OPENSSL req -x509 -nodes -days 3650 \
    -newkey rsa:4096 \
    -keyout "$KEY_FILE" \
    -out    "$CERT_FILE" \
    -subj   "/C=TR/ST=Istanbul/O=NIZAM-COP/CN=${TLS_CN}" \
    -addext "subjectAltName=DNS:${TLS_CN},DNS:localhost,IP:127.0.0.1" \
    2>/dev/null

  chmod 600 "$KEY_FILE"
  ok "TLS certificate generated"
  echo -e "    cert : $CERT_FILE"
  echo -e "    key  : $KEY_FILE"
fi

# ── 4. Mosquitto certs dir ───────────────────────────────────────────────────
MOSQ_CERT_DIR="$REPO_ROOT/mosquitto/certs"
if [[ ! -d "$MOSQ_CERT_DIR" ]]; then
  mkdir -p "$MOSQ_CERT_DIR"
  ok "Created mosquitto/certs/ directory"
fi

# ── 5. Summary ───────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}Setup complete.${NC}"
echo ""
echo -e "  .env                    $ENV_FILE"
echo -e "  TLS cert                $CERT_FILE"
echo -e "  TLS key                 $KEY_FILE"
echo ""
echo -e "${YELLOW}Security reminders:${NC}"
echo -e "  • Review .env — never commit it to version control"
echo -e "  • Replace self-signed cert with a CA-signed cert for public deployments"
echo -e "  • Ensure ports 80/443/1883 are firewalled to intended networks only"
echo -e "  • AUTH_ENABLED=true is set by default in the prod config"
echo ""

# ── 6. Optionally start stack ────────────────────────────────────────────────
if [[ "$START_STACK" == true ]]; then
  command -v docker >/dev/null 2>&1 || die "docker not available — cannot start stack"

  COMPOSE_FILE="$REPO_ROOT/docker-compose.prod.yml"
  [[ -f "$COMPOSE_FILE" ]] || die "docker-compose.prod.yml not found"

  info "Starting production stack…"
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --build

  echo ""
  ok "Stack started. Services:"
  docker compose -f "$COMPOSE_FILE" ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || true
  echo ""
  echo -e "${GREEN}COP interface:${NC}  https://localhost  (or https://${TLS_CN})"
  echo -e "${GREEN}MQTT broker:${NC}    mqtt://localhost:1883"
  echo ""
  info "Tail logs: docker compose -f docker-compose.prod.yml logs -f cop"
fi
