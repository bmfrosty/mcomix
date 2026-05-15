#!/usr/bin/env bash
# Install MComix from source for the current user (no Flatpak, no root).
# Run from the repo root.  Re-running is safe (idempotent).

set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
SHARE="$HOME/.local/share"

# Find a Python 3 that has gi (PyGObject).  Prefer /usr/bin/python3 over
# anything in a brew/venv prefix since the system RPM packages install there.
PYTHON=""
for candidate in /usr/bin/python3 python3; do
    if "$candidate" -c "import gi" 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done
if [[ -z "$PYTHON" ]]; then
    echo "ERROR: no Python 3 with PyGObject (gi) found."
    echo "Install with:  sudo dnf install python3-gobject"
    exit 1
fi
echo "==> Using Python: $PYTHON ($($PYTHON --version))"

echo "==> Checking required Python packages..."
for mod in gi PIL; do
    if ! "$PYTHON" -c "import $mod" 2>/dev/null; then
        echo "ERROR: Python module '$mod' not found in $PYTHON."
        [[ "$mod" == "PIL" ]] && echo "Install with:  sudo dnf install python3-pillow"
        exit 1
    fi
done
echo "    OK"

echo "==> Installing launcher to $BIN/mcomix..."
mkdir -p "$BIN"
cat > "$BIN/mcomix" <<EOF
#!/usr/bin/env bash
exec "$PYTHON" "$REPO/mcomixstarter.py" "\$@"
EOF
chmod +x "$BIN/mcomix"

echo "==> Installing .desktop file..."
mkdir -p "$SHARE/applications"
sed "s|Exec=mcomix %u|Exec=$BIN/mcomix %u|" \
    "$REPO/share/applications/mcomix.desktop" \
    > "$SHARE/applications/mcomix.desktop"

echo "==> Installing icons..."
while IFS= read -r -d '' src; do
    rel="${src#$REPO/share/icons/}"
    dest="$SHARE/icons/$rel"
    mkdir -p "$(dirname "$dest")"
    cp -f "$src" "$dest"
done < <(find "$REPO/share/icons" -type f -print0)

echo "==> Installing MIME types..."
mkdir -p "$SHARE/mime/packages"
cp -f "$REPO/share/mime/packages/mcomix.xml" "$SHARE/mime/packages/mcomix.xml"
update-mime-database "$SHARE/mime" 2>/dev/null || true

echo "==> Updating desktop database..."
update-desktop-database "$SHARE/applications" 2>/dev/null || true
gtk-update-icon-cache -f -t "$SHARE/icons/hicolor" 2>/dev/null || true

echo ""
echo "Done."
echo "Run:  mcomix"
