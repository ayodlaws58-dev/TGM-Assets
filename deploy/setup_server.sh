#!/usr/bin/env bash
# One-time setup of TGM Assets on an Ubuntu 22.04/24.04 server, served through Cloudflare Tunnel.
# Run from the app checkout at /opt/tgm-assets:   sudo bash deploy/setup_server.sh
# Safe to run again: it keeps an existing .env, database and user.
set -euo pipefail

APP_DIR=/opt/tgm-assets
APP_USER=tgm
DB_NAME=tgm_assets
DB_USER=tgm

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
[ "$(pwd)" = "$APP_DIR" ] || { echo "Run from $APP_DIR (the app checkout)." >&2; exit 1; }

echo "==> Installing packages"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q mysql-server python3-venv python3-pip ufw curl

echo "==> Creating the $APP_USER system user"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

echo "==> Keeping MySQL on localhost only"
cat > /etc/mysql/mysql.conf.d/zz-tgm-assets.cnf <<'EOF'
[mysqld]
bind-address = 127.0.0.1
mysqlx-bind-address = 127.0.0.1
character-set-server = utf8mb4
EOF
systemctl enable --now mysql
systemctl restart mysql

echo "==> Writing .env (kept if it already exists)"
if [ ! -f "$APP_DIR/.env" ]; then
  DB_PASS=$(openssl rand -hex 24)
  cat > "$APP_DIR/.env" <<EOF
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=$DB_NAME
MYSQL_USER=$DB_USER
MYSQL_PASSWORD=$DB_PASS
SECRET_KEY=$(openssl rand -hex 32)
TRUST_PROXY=1
APP_BASE_URL=https://assets.tgmeducation.com
# Fill these in, then: sudo systemctl restart tgm-assets
RESEND_API_KEY=
RESEND_FROM_EMAIL=frontdesk@tgmeducation.net
EOF
fi
chmod 600 "$APP_DIR/.env"

env_value() { grep -E "^$1=" "$APP_DIR/.env" | head -n1 | cut -d= -f2-; }
DB_PASS=$(env_value MYSQL_PASSWORD)

echo "==> Creating database $DB_NAME and user $DB_USER"
mysql <<EOF
CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
CREATE USER IF NOT EXISTS '$DB_USER'@'127.0.0.1' IDENTIFIED BY '$DB_PASS';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'localhost';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'127.0.0.1';
FLUSH PRIVILEGES;
EOF

echo "==> Installing Python dependencies"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

mkdir -p "$APP_DIR/data/asset_uploads" /var/backups/tgm-assets
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 700 /var/backups/tgm-assets

echo "==> Installing systemd units"
install -m 644 deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
install -m 755 deploy/backup.sh /usr/local/bin/tgm-assets-backup
systemctl daemon-reload
systemctl enable --now tgm-assets.service tgm-reminders.timer tgm-backup.timer

echo "==> Installing cloudflared"
if ! command -v cloudflared >/dev/null; then
  mkdir -p --mode=0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
    > /etc/apt/sources.list.d/cloudflared.list
  apt-get update -q && apt-get install -y -q cloudflared
fi

echo "==> Firewall: SSH only (the tunnel needs no open ports)"
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null

cat <<EOF

Done. The app is listening on 127.0.0.1:8000 and is not reachable from the internet yet.
Next:
  1. Put RESEND_API_KEY in $APP_DIR/.env, then: sudo systemctl restart tgm-assets
  2. Import data (first time only):
       sudo -u $APP_USER $APP_DIR/.venv/bin/python scripts/import_csv_to_mysql.py --dry-run
       sudo -u $APP_USER $APP_DIR/.venv/bin/python scripts/import_csv_to_mysql.py
  3. Create the tunnel in the Cloudflare dashboard and run its install command here:
       sudo cloudflared service install <TOKEN>
     Public hostname: assets.tgmeducation.com  ->  http://127.0.0.1:8000
EOF
