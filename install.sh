#!/bin/bash
# =============================================================================
#  BriskHeat LYNX Dashboard – Raspberry Pi Installer
#  Run as a regular user (not root). sudo is invoked only where needed.
# =============================================================================

set -euo pipefail

# ── colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()     { error "$*"; exit 1; }
header()  { echo -e "\n${BOLD}${CYAN}══════════════════════════════════════════${NC}"; \
            echo -e "${BOLD}${CYAN}  $*${NC}"; \
            echo -e "${BOLD}${CYAN}══════════════════════════════════════════${NC}"; }

# ── defaults ───────────────────────────────────────────────────────────────────
INSTALL_DIR="${INSTALL_DIR:-/home/$(logname)/lynx}"
SERVICE_USER="$(logname)"
SERVICE_NAME="lynx-dashboard"
PYTHON_MIN="3.9"
PG_DB="lynx"
PG_USER="lynx_user"
PG_PASS="${PG_PASS:-$(openssl rand -base64 16 | tr -dc 'a-zA-Z0-9' | head -c 16)}"

# ── check not running as root ──────────────────────────────────────────────────
if [[ $EUID -eq 0 ]]; then
  die "Do not run this script as root. Run as your normal user; sudo will be used where needed."
fi

header "BriskHeat LYNX Dashboard – Raspberry Pi Install"
echo ""
echo "  Install directory : $INSTALL_DIR"
echo "  Service user      : $SERVICE_USER"
echo "  Service name      : $SERVICE_NAME"
echo "  PostgreSQL DB     : $PG_DB"
echo "  PostgreSQL user   : $PG_USER"
echo ""

read -rp "$(echo -e "${BOLD}Proceed? [y/N]:${NC} ")" CONFIRM
[[ "${CONFIRM,,}" == "y" ]] || { info "Aborted."; exit 0; }

# ── 1. System packages ─────────────────────────────────────────────────────────
header "Step 1 – System packages"

info "Updating package list…"
sudo apt-get update -qq

info "Installing system dependencies…"
sudo apt-get install -y -qq \
    python3 python3-pip python3-venv \
    postgresql postgresql-contrib \
    libpq-dev \
    git curl

PYTHON_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Python version: $PYTHON_VER"

python3 -c "
import sys
major, minor = sys.version_info[:2]
req_major, req_minor = map(int, '${PYTHON_MIN}'.split('.'))
if (major, minor) < (req_major, req_minor):
    print(f'Python {req_major}.{req_minor}+ required, found {major}.{minor}')
    sys.exit(1)
" || die "Python ${PYTHON_MIN}+ is required."

success "System packages installed."

# ── 2. Install directory ───────────────────────────────────────────────────────
header "Step 2 – Install directory"

if [[ ! -d "$INSTALL_DIR" ]]; then
    info "Creating $INSTALL_DIR …"
    mkdir -p "$INSTALL_DIR"
else
    warn "Directory $INSTALL_DIR already exists – files will be overwritten."
fi

# Copy app files from the directory containing this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info "Copying application files from $SCRIPT_DIR …"

for f in app.py lynx_dashboard.py lynx_db_logger.py lynx_history.py run.sh config.ini.example \
          lynx_schedule.py lynx_reader.py lynx_set_all.py test_connection.py; do
    if [[ -f "$SCRIPT_DIR/$f" ]]; then
        cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/"
        success "Copied $f"
    else
        warn "Missing: $f (skipped)"
    fi
done

# Copy templates directory
if [[ -d "$SCRIPT_DIR/templates" ]]; then
    cp -r "$SCRIPT_DIR/templates" "$INSTALL_DIR/"
    success "Copied templates/"
else
    warn "templates/ directory not found"
fi

[[ -f "$INSTALL_DIR/run.sh" ]] && chmod +x "$INSTALL_DIR/run.sh"
success "Files installed to $INSTALL_DIR"

# ── 3. Python virtual environment ─────────────────────────────────────────────
header "Step 3 – Python virtual environment"

VENV_DIR="$INSTALL_DIR/venv"
if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating virtual environment at $VENV_DIR …"
    python3 -m venv "$VENV_DIR"
fi

info "Installing Python packages…"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet \
    flask \
    flask-socketio \
    pymodbus \
    eventlet \
    psycopg2-binary

# Verify packages are importable from the venv
info "Verifying venv packages..."
"$VENV_DIR/bin/python" -c "import flask, flask_socketio, pymodbus, eventlet, psycopg2" \
    || die "Package verification failed. Check pip output above."
success "All packages verified in venv."

success "Python packages installed."

# ── 4. PostgreSQL ──────────────────────────────────────────────────────────────
header "Step 4 – PostgreSQL"

info "Ensuring PostgreSQL is started…"
sudo systemctl enable postgresql --quiet
sudo systemctl start  postgresql

# Create DB user
info "Creating PostgreSQL user '$PG_USER'…"
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$PG_USER'" | grep -q 1; then
    info "User '$PG_USER' already exists – updating password."
    sudo -u postgres psql -c "ALTER USER $PG_USER WITH PASSWORD '$PG_PASS';" > /dev/null
else
    sudo -u postgres psql -c "CREATE USER $PG_USER WITH PASSWORD '$PG_PASS';" > /dev/null
    success "Created user '$PG_USER'."
fi

# Create database
info "Creating database '$PG_DB'…"
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$PG_DB'" | grep -q 1; then
    info "Database '$PG_DB' already exists."
else
    sudo -u postgres psql -c "CREATE DATABASE $PG_DB OWNER $PG_USER;" > /dev/null
    success "Created database '$PG_DB'."
fi

# Grant privileges
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $PG_DB TO $PG_USER;" > /dev/null
sudo -u postgres psql -d "$PG_DB" -c "GRANT ALL ON SCHEMA public TO $PG_USER;" > /dev/null
success "PostgreSQL configured."

# ── Serial port group (for RTU / Centipede 2) ──────────────────────────────
if getent group dialout > /dev/null 2>&1; then
    info "Adding $SERVICE_USER to dialout group (serial port access)…"
    sudo usermod -aG dialout "$SERVICE_USER"
    success "User added to dialout group (re-login or reboot to take effect)."
fi

# ── 5. config.ini ──────────────────────────────────────────────────────────────
header "Step 5 – config.ini"

CONFIG_FILE="$INSTALL_DIR/config.ini"

# Detect local timezone
LOCAL_TZ="UTC"
if [[ -f /etc/timezone ]]; then
    LOCAL_TZ=$(cat /etc/timezone)
elif command -v timedatectl &>/dev/null; then
    LOCAL_TZ=$(timedatectl show -p Timezone --value 2>/dev/null || echo "UTC")
fi
info "Detected timezone: $LOCAL_TZ"

if [[ -f "$CONFIG_FILE" ]]; then
    warn "config.ini already exists – creating config.ini.new instead (using config.ini.example as template)."
    CONFIG_TARGET="$INSTALL_DIR/config.ini.new"
else
    CONFIG_TARGET="$CONFIG_FILE"
fi

# Get OI Gateway IP from user
echo ""
read -rp "$(echo -e "${BOLD}OI Gateway IP address${NC} [192.168.200.20]: ")" OI_HOST
OI_HOST="${OI_HOST:-192.168.200.20}"

cat > "$CONFIG_TARGET" << CFGEOF
[modbus]
host          = $OI_HOST
port          = 502
timeout       = 4.0
line_voltage  = 240
scan_interval = 8
lines         = 1,2,3,4

[database]
host            = localhost
port            = 5432
name            = $PG_DB
user            = $PG_USER
password        = $PG_PASS
log_interval    = 60
purge_threshold = 80
purge_keep_pct  = 60
admin_user      = postgres
admin_password  =

[flask]
secret_key = $(openssl rand -base64 24 | tr -dc 'a-zA-Z0-9' | head -c 24)
port       = 5000

[dashboard]
ip       = 127.0.0.1
timeout  = 8
timezone = $LOCAL_TZ
CFGEOF

success "config.ini written to $CONFIG_TARGET"

if [[ "$CONFIG_TARGET" != "$CONFIG_FILE" ]]; then
    warn "Review $CONFIG_TARGET and rename it to config.ini when ready."
fi

# ── 6. systemd service ─────────────────────────────────────────────────────────
header "Step 6 – systemd service"

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

info "Writing $SERVICE_FILE …"
sudo tee "$SERVICE_FILE" > /dev/null << SVCEOF
[Unit]
Description=BriskHeat LYNX Dashboard
Documentation=https://github.com/pangchi/lynx_dashboard
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=$SERVICE_USER
Group=dialout
SupplementaryGroups=dialout
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/python $INSTALL_DIR/app.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=lynx-dashboard

# Environment — explicit paths so systemd's clean env matches run.sh
Environment=PYTHONUNBUFFERED=1
Environment=VIRTUAL_ENV=$VENV_DIR
Environment=PATH=$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=/home/$SERVICE_USER
Environment=LYNX_APP_DIR=$INSTALL_DIR

[Install]
WantedBy=multi-user.target
SVCEOF

info "Reloading systemd daemon…"
sudo systemctl daemon-reload

info "Enabling service to start on boot…"
sudo systemctl enable "${SERVICE_NAME}" --quiet

success "Service installed: $SERVICE_NAME"

# ── 7. Firewall (optional) ─────────────────────────────────────────────────────
header "Step 7 – Firewall"

if command -v ufw &>/dev/null; then
    UFW_STATUS=$(sudo ufw status | head -1)
    if [[ "$UFW_STATUS" == *"active"* ]]; then
        info "Opening port 5000 in ufw…"
        sudo ufw allow 5000/tcp comment "LYNX Dashboard" > /dev/null
        success "Port 5000 opened."
    else
        info "ufw is installed but not active – skipping."
    fi
else
    info "ufw not found – skipping firewall configuration."
fi

# ── 8. Test connection ─────────────────────────────────────────────────────────
header "Step 8 – Test connection"

if [[ -f "$CONFIG_FILE" ]]; then
    info "Running test_connection.py…"
    cd "$INSTALL_DIR"
    "$VENV_DIR/bin/python" test_connection.py || warn "Connection test reported issues – check output above."
else
    warn "config.ini not finalised yet – skipping connection test."
fi

# ── 9. Start service ───────────────────────────────────────────────────────────
header "Step 9 – Start service"

read -rp "$(echo -e "${BOLD}Start the LYNX dashboard service now? [Y/n]:${NC} ")" START_NOW
if [[ "${START_NOW,,}" != "n" ]]; then
    sudo systemctl start "${SERVICE_NAME}"
    sleep 3
    STATUS=$(sudo systemctl is-active "${SERVICE_NAME}" || true)
    if [[ "$STATUS" == "active" ]]; then
        success "Service is running."
    else
        warn "Service status: $STATUS"
        warn "Check logs with:  sudo journalctl -u ${SERVICE_NAME} -n 50"
    fi
fi

# ── summary ────────────────────────────────────────────────────────────────────
PI_IP=$(hostname -I | awk '{print $1}')

header "Installation Complete"
echo ""
echo -e "  ${BOLD}Dashboard${NC}     → http://${PI_IP}:5000"
echo -e "  ${BOLD}History${NC}       → http://${PI_IP}:5000/history"
echo -e "  ${BOLD}Energy${NC}        → http://${PI_IP}:5000/energy"
echo -e "  ${BOLD}Schedule${NC}      → http://${PI_IP}:5000/schedule"
echo -e "  ${BOLD}Settings${NC}      → http://${PI_IP}:5000/settings"
echo ""
echo -e "  ${BOLD}Install dir${NC}   : $INSTALL_DIR"
echo -e "  ${BOLD}Config file${NC}   : $CONFIG_FILE"
echo -e "  ${BOLD}Service${NC}       : $SERVICE_NAME"
echo ""
echo -e "  ${BOLD}Useful commands:${NC}"
echo "    sudo systemctl status  ${SERVICE_NAME}"
echo "    sudo systemctl restart ${SERVICE_NAME}"
echo "    sudo journalctl -u ${SERVICE_NAME} -f"
  echo ""
  echo -e "  ${BOLD}Manual start (no service):${NC}"
  echo "    cd $INSTALL_DIR && ./run.sh"
echo ""
echo -e "  ${BOLD}DB credentials${NC} (saved in config.ini):"
echo "    Host     : localhost"
echo "    Database : $PG_DB"
echo "    User     : $PG_USER"
echo "    Password : $PG_PASS"
echo ""
echo -e "${GREEN}${BOLD}Done.${NC}"
