#!/bin/bash
#
# Aetrix Portal Database Restore Script
#
# This script restores a PostgreSQL database from a backup file
#
# Usage: ./restore_db.sh <database_name> <backup_file>
#
# 默认连接宿主机 PostgreSQL。示例库名来自旧拆分栈，不要用于现行的
# aetrix-postgres 容器，除非调用者改完 DB_HOST、DB_USER、DB_NAME 并接受删库。
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check arguments
if [ $# -ne 2 ]; then
    log_error "Usage: $0 <database_name> <backup_file>"
    echo ""
    echo "Example: $0 portal_user /root/Aetrix-Portal/backups/daily/portal_user_daily_20240101_020000.sql.gz"
    exit 1
fi

DB_NAME=$1
BACKUP_FILE=$2

# Validate backup file exists
if [ ! -f "$BACKUP_FILE" ]; then
    log_error "Backup file not found: $BACKUP_FILE"
    exit 1
fi

# Get database credentials
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-postgres}"
DB_PASSWORD="${POSTGRES_PASSWORD:-}"

if [ -z "$DB_PASSWORD" ]; then
    log_error "POSTGRES_PASSWORD environment variable not set"
    exit 1
fi

export PGPASSWORD="$DB_PASSWORD"

# Warning prompt
log_warn "========================================="
log_warn "WARNING: This will restore database '$DB_NAME'"
log_warn "from backup file:"
log_warn "  $BACKUP_FILE"
log_warn ""
log_warn "This will OVERWRITE all existing data in the database!"
log_warn "========================================="
echo ""
read -p "Are you sure you want to continue? (type 'yes' to confirm): " confirmation

if [ "$confirmation" != "yes" ]; then
    log_info "Restore cancelled."
    exit 0
fi

log_info "Starting database restore..."
log_info "Target database: $DB_NAME"
log_info "Backup file: $BACKUP_FILE"

# Drop and recreate database
log_info "Dropping existing database (if exists)..."
dropdb -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME" 2>/dev/null || true

log_info "Creating new database..."
createdb -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME"

# Restore from backup
log_info "Restoring data from backup..."
if gunzip -c "$BACKUP_FILE" | psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME"; then
    log_info "========================================="
    log_info "Database restore completed successfully!"
    log_info "========================================="
else
    log_error "Failed to restore database!"
    exit 1
fi
