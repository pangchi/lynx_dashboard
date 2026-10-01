#!/bin/bash
# =============================================================================
#  BriskHeat LYNX Dashboard – Uninstaller
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()     { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] && die "Do not run as root."

INSTALL_DIR="${INSTALL_DIR:-/home/$(logname)/lynx}"
SERVICE_NAME="lynx-dashboard"
PG_DB="lynx"
PG_USER="lynx_user"

echo -e "\n${BOLD}${RED}BriskHeat LYNX Dashboard – Uninstall${NC}\n"
echo "  This will:"
echo "    • Stop and remove the systemd service"
echo "    • Remove $INSTALL_DIR"
echo "    • Optionally drop the PostgreSQL database and user"
echo ""
read -rp "$(echo -e "${BOLD}Proceed? [y/N]:${NC} ")" CONFIRM
[[ "${CONFIRM,,}" == "y" ]] || { info "Aborted."; exit 0; }

# Stop and disable service
if systemctl list-unit-files | grep -q "${SERVICE_NAME}.service"; then
    info "Stopping service…"
    sudo systemctl stop    "${SERVICE_NAME}" 2>/dev/null || true
    sudo systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    sudo systemctl daemon-reload
    success "Service removed."
fi

# Remove install directory
if [[ -d "$INSTALL_DIR" ]]; then
    read -rp "$(echo -e "${BOLD}Remove $INSTALL_DIR? [y/N]:${NC} ")" REM_DIR
    if [[ "${REM_DIR,,}" == "y" ]]; then
        rm -rf "$INSTALL_DIR"
        success "Removed $INSTALL_DIR"
    else
        info "Skipped directory removal."
    fi
fi

# Drop PostgreSQL database and user
read -rp "$(echo -e "${BOLD}Drop PostgreSQL database '$PG_DB' and user '$PG_USER'? [y/N]:${NC} ")" DROP_DB
if [[ "${DROP_DB,,}" == "y" ]]; then
    sudo -u postgres psql -c "DROP DATABASE IF EXISTS $PG_DB;" > /dev/null && \
        success "Dropped database '$PG_DB'."
    sudo -u postgres psql -c "DROP USER IF EXISTS $PG_USER;"    > /dev/null && \
        success "Dropped user '$PG_USER'."
fi

# Remove ufw rule
if command -v ufw &>/dev/null; then
    sudo ufw delete allow 5000/tcp 2>/dev/null || true
fi

echo -e "\n${GREEN}${BOLD}Uninstall complete.${NC}\n"
