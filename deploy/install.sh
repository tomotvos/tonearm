#!/bin/bash
set -euo pipefail

SERVICE_USER="${1:?usage: sudo deploy/install.sh <service-user>}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
OWNTONE_APT=https://raw.githubusercontent.com/owntone/owntone-apt/refs/heads/master/repo/rpi

install_owntone() {
    . /etc/os-release
    case "$(dpkg --print-architecture)" in
        arm64|armhf) ;;
        *)
            echo "OwnTone is not installed, and its apt repository only covers Raspberry Pi OS." >&2
            echo "Install OwnTone first: https://owntone.github.io/owntone-server/installation/" >&2
            exit 1
            ;;
    esac
    apt-get install -y ca-certificates curl gpg
    curl -fsSL "$OWNTONE_APT/owntone.gpg" | gpg --dearmor --yes --output /usr/share/keyrings/owntone-archive-keyring.gpg
    if ! curl -fsSL -o /etc/apt/sources.list.d/owntone.list "$OWNTONE_APT/owntone-$VERSION_CODENAME.list"; then
        rm -f /etc/apt/sources.list.d/owntone.list
        echo "OwnTone has no Raspberry Pi OS repository for '$VERSION_CODENAME'." >&2
        exit 1
    fi
    apt-get update
    apt-get install -y owntone
}

apt-get update
if ! dpkg -s owntone >/dev/null 2>&1; then
    install_owntone
fi
apt-get install -y python3-aiohttp alsa-utils
usermod -aG audio "$SERVICE_USER"

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" /opt/tonearm
cp -r "$SRC/tonearm" "$SRC/web" /opt/tonearm/
chown -R "$SERVICE_USER:$SERVICE_USER" /opt/tonearm

mkdir -p /srv/music
[ -p /srv/music/Turntable ] || mkfifo /srv/music/Turntable
chown "$SERVICE_USER:$SERVICE_USER" /srv/music/Turntable
chmod 0644 /srv/music/Turntable

sed -i -E 's/^[[:space:]]*#?[[:space:]]*pipe_autostart[[:space:]]*=.*/\tpipe_autostart = false/' /etc/owntone.conf
grep -qE '^[[:space:]]*pipe_autostart[[:space:]]*=[[:space:]]*false' /etc/owntone.conf

sed "s/@USER@/$SERVICE_USER/" "$SRC/deploy/tonearm.service" > /etc/systemd/system/tonearm.service
systemctl daemon-reload
systemctl enable owntone
systemctl restart owntone
systemctl enable tonearm
systemctl restart tonearm
systemctl --no-pager status tonearm
