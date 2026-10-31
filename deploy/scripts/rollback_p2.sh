#!/usr/bin/env bash
# SUI-Quant P2 One-Command Rollback Script (Rolls back F-01 collector while preserving P1 services)
set -euo pipefail

echo "Stopping SUI-Quant P2 F-01 Market Collector service..."
sudo systemctl stop sui-quant-f01.service || true
sudo systemctl disable sui-quant-f01.service || true
sudo rm -f /etc/systemd/system/sui-quant-f01.service
sudo systemctl daemon-reload
echo "SUI-Quant P2 F-01 service stopped and rolled back cleanly; P1 services remain active."
