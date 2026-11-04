#!/usr/bin/env bash
# Install omarchy-info-panel: dependencies, udev rule, default config, systemd user service.
# Safe to re-run: an existing config is never overwritten.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_DIR="$HOME/.config/lcdd"
CONF="$CONF_DIR/config.toml"
UNIT="$HOME/.config/systemd/user/lcdd.service"
RULE=/etc/udev/rules.d/99-ax206.rules

die() { echo "error: $*" >&2; exit 1; }

[[ $EUID -ne 0 ]] || die "run as your normal user, not root (sudo is used where needed)"
command -v pacman >/dev/null || die "this installer is for Arch Linux (pacman not found)"

echo "==> Installing packages (python-pyusb, python-pillow)"
sudo pacman -S --needed python-pyusb python-pillow

# Skip when the file exists: another Nerd Font package (e.g. ttf-jetbrains-mono-nerd-basic) may provide it and conflict.
if [[ ! -f /usr/share/fonts/TTF/JetBrainsMonoNerdFont-Regular.ttf ]]; then
  echo "==> Installing JetBrains Mono Nerd Font"
  sudo pacman -S --needed ttf-jetbrains-mono-nerd
fi

echo "==> Installing udev rule (lets your user talk to the LCD without sudo)"
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="1908", ATTR{idProduct}=="0102", MODE="0666"' | sudo tee "$RULE" >/dev/null
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=usb

echo "==> Config"
if [[ -f $CONF ]]; then
  echo "    $CONF already exists, leaving it alone"
else
  mkdir -p "$CONF_DIR"
  cat >"$CONF" <<'EOF'
# omarchy-info-panel config. Apply changes with: systemctl --user reload lcdd

layout         = "rows"     # "rows" | "tiles"
theme          = "omarchy"  # "omarchy" (follow the Omarchy theme) | "neutral" (built-in colours)
refresh        = 1.0        # seconds between updates, minimum 0.2
brightness     = 7          # backlight 0-7 (0 = off, 7 = full)
language       = "en"       # "en" | "it"
show_weather   = true       # icon, temperature and condition after the clock
show_tailscale = true       # dot + "TS <peers online>" on the network line
weather_city   = ""         # e.g. "Vicenza"; empty = use the Omarchy weather location
EOF
  echo "    created $CONF"
fi

echo "==> systemd user service"
mkdir -p "$(dirname "$UNIT")"
sed "s|@DIR@|$DIR|g" "$DIR/lcdd.service" >"$UNIT"
systemctl --user daemon-reload
systemctl --user enable lcdd
systemctl --user restart lcdd

lsusb | grep -q '1908:0102' || echo "warning: LCD (1908:0102) not detected. Plug it in; the service retries every few seconds."
echo "Done. Status: systemctl --user status lcdd   Logs: journalctl --user -u lcdd -f"
