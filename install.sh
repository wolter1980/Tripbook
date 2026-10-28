#!/bin/sh
# Installs the Tripbook launcher, menu entry and icon for the current user.
# The udev rule (tracker access without the dialout group) needs root: run with --udev.
set -e
HERE="$(dirname "$(readlink -f "$0")")"
mkdir -p ~/.local/bin ~/.local/share/applications ~/.local/share/icons/hicolor/scalable/apps
ln -sf "$HERE/bin/tripbook" ~/.local/bin/tripbook
cp "$HERE/data/tripbook.desktop" ~/.local/share/applications/tripbook.desktop
cp "$HERE/tripbook/tripbook.svg" ~/.local/share/icons/hicolor/scalable/apps/tripbook.svg
update-desktop-database ~/.local/share/applications 2>/dev/null || true
# Desktop shortcut (absolute paths so it works even if ~/.local/bin is not on the session PATH)
DESKTOP="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESKTOP" ]; then
    sed -e "s|^Exec=tripbook|Exec=$HOME/.local/bin/tripbook|" \
        -e "s|^Icon=tripbook|Icon=$HOME/.local/share/icons/hicolor/scalable/apps/tripbook.svg|" \
        "$HERE/data/tripbook.desktop" > "$DESKTOP/tripbook.desktop"
    chmod +x "$DESKTOP/tripbook.desktop"
fi
echo "Installed: run 'tripbook' or find Tripbook in the application menu."
if [ "$1" = "--udev" ]; then
    sudo install -m 644 "$HERE/data/70-tripbook-tracker.rules" /etc/udev/rules.d/
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=tty
    echo "udev rule installed: unplug and replug the tracker."
fi
