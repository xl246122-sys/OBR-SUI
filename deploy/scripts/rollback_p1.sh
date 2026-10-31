#!/usr/bin/env bash
# SUI-Quant P1 One-Command Rollback Script
set -euo pipefail

echo "Stopping SUI-Quant P1 systemd services..."
sudo systemctl stop sui-quant-guardian.service sui-quant-r01.service || true
sudo systemctl disable sui-quant-guardian.service sui-quant-r01.service || true
sudo rm -f /etc/systemd/system/sui-quant-*.service
sudo systemctl daemon-reload
echo "SUI-Quant P1 services stopped and removed cleanly."
