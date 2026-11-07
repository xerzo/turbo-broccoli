#!/usr/bin/env bash
# ==============================================================================
# Production Deployment & Update Script
#
# Pulls pre-built images from GitHub Container Registry (GHCR) and restarts
# services without rebuilding locally.
# ==============================================================================

set -euo pipefail

# ANSI Color codes
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# 1. Change to script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 2. Load .env if present
if [ -f ".env" ]; then
    log_info "Loading configuration from .env file..."
    set -a
    source .env
    set +a
fi

# 3. Verify Docker & Compose
if ! command -v docker &>/dev/null; then
    log_error "Docker is not installed or not found in PATH."
    exit 1
fi

DOCKER_COMPOSE_CMD=""
if docker compose version &>/dev/null; then
    DOCKER_COMPOSE_CMD="docker compose"
elif command -v docker-compose &>/dev/null; then
    DOCKER_COMPOSE_CMD="docker-compose"
else
    log_error "Neither 'docker compose' nor 'docker-compose' was found."
    exit 1
fi

# 4. Check external network 'tg_net'
if ! docker network inspect tg_net &>/dev/null; then
    log_warn "Network 'tg_net' does not exist. Creating it now..."
    docker network create tg_net
    log_success "Created network 'tg_net'"
fi

# 5. Prevent Docker bind-mount file-vs-directory collision on tg-feed-bot.db
if [ -d "tg-comment/tg-feed-bot.db" ]; then
    if [ -z "$(ls -A tg-comment/tg-feed-bot.db 2>/dev/null)" ]; then
        log_warn "Detected 'tg-comment/tg-feed-bot.db' as an empty directory from a previous Docker mount. Replacing with file..."
        rmdir "tg-comment/tg-feed-bot.db"
        touch "tg-comment/tg-feed-bot.db"
    fi
elif [ ! -e "tg-comment/tg-feed-bot.db" ]; then
    mkdir -p tg-comment
    touch "tg-comment/tg-feed-bot.db"
fi

# 6. Backup persistent state (SQLite DB & session) before updating
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_DIR="${SCRIPT_DIR}/backups/${TIMESTAMP}"

if [ -f "tg-comment/tg-feed-bot.db" ] || [ -d "anisearch/session" ]; then
    log_info "Backing up persistent database and session files to ${BACKUP_DIR}..."
    mkdir -p "${BACKUP_DIR}"
    if [ -f "tg-comment/tg-feed-bot.db" ]; then
        cp -p "tg-comment/tg-feed-bot.db" "${BACKUP_DIR}/"
    fi
    if [ -d "anisearch/session" ]; then
        cp -rp "anisearch/session" "${BACKUP_DIR}/"
    fi
    log_success "State backup completed."
fi

# 7. Pull latest images
log_info "Pulling pre-built images from registry..."
$DOCKER_COMPOSE_CMD pull

# 8. Recreate containers with minimal downtime
log_info "Deploying and restarting updated containers..."
$DOCKER_COMPOSE_CMD up -d --remove-orphans

# 9. Clean up unused images
log_info "Pruning old and dangling container images..."
docker image prune -f

# 10. Status report
log_success "Deployment completed successfully! Current service status:"
$DOCKER_COMPOSE_CMD ps
