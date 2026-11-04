#!/usr/bin/env bash
# Run once on a new Ubuntu server, as a sudo user. Installs Docker and Tailscale,
# sets Chicago time so the schedule runs at the right hours, and adds swap for builds.
set -euo pipefail
sudo timedatectl set-timezone America/Chicago
if ! swapon --show | grep -q .; then
  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sudo sh
sudo apt-get update -qq && sudo apt-get install -y -qq git sqlite3 python3 cron
sudo systemctl enable --now cron docker
echo "Done. Log out and back in so Docker works without sudo."
