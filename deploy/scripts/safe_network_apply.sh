#!/usr/bin/env bash
# SUI-Quant P1 4-Step Anti-Lockout Network & Firewall Change Script
set -euo pipefail

BACKUP_DIR="/var/backups/sui-quant"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="${BACKUP_DIR}/net-rollback-${TS}.tar.gz"
RESTORE_SCRIPT="${BACKUP_DIR}/restore_latest_net.sh"

sudo mkdir -p "${BACKUP_DIR}"

echo "[Step 1/4] Creating timestamped backup of /etc/ssh, /etc/ufw, /etc/fail2ban -> ${ARCHIVE}"
sudo tar -czf "${ARCHIVE}" /etc/ssh /etc/ufw /etc/fail2ban
sudo sha256sum "${ARCHIVE}" | sudo tee "${ARCHIVE}.sha256"

sudo tee "${RESTORE_SCRIPT}" > /dev/null << EOF
#!/usr/bin/env bash
set -e
tar -xzf "${ARCHIVE}" -C /
systemctl restart ssh
ufw reload || true
systemctl restart fail2ban || true
EOF
sudo chmod 0700 "${RESTORE_SCRIPT}"

echo "[Step 2/4] Verifying out-of-band serial console (serial-getty@ttyS0.service) is active"
systemctl is-active serial-getty@ttyS0.service

echo "[Step 3/4] Checking sshd syntax before arming 180s auto-rollback timer"
sudo sshd -t
sudo systemd-run --unit=sui-net-rollback --on-active=180s /bin/bash "${RESTORE_SCRIPT}"

echo "[Step 4/4] 180s rollback timer armed. Verify a SECOND independent SSH session works, then run:"
echo "  sudo systemctl stop sui-net-rollback.timer"
