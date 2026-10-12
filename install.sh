#!/usr/bin/env bash
# Install on Arch / Omarchy by building a proper pacman package from this folder.
set -euo pipefail
cd "$(dirname "$0")"

echo ":: Installing build/runtime dependencies (sudo)…"
sudo pacman -S --needed --noconfirm base-devel python python-gobject gtk4 libadwaita ffmpeg udisks2 \
  python-build python-installer python-setuptools python-wheel python-pytest

echo ":: Building and installing the package…"
makepkg -si --noconfirm --cleanbuild

echo ":: Reloading udev rules so your user can talk to the RØDECaster…"
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw

cat <<'EOF'

Done.
  • Unplug and replug the RØDECaster Pro's USB cable once.
  • Launch "RØDECaster Pro" from the app launcher (Super + Space), or run:  rodecaster
  • Command line:  rodecaster-cli --help        (try: rodecaster-cli info)
  • No hardware handy?  rodecaster --simulate
EOF
