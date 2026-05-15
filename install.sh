#!/usr/bin/env bash
# install.sh — set up OpenPnP scripts and pnp_creator tool
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS_SRC="$REPO_DIR/scripts"
TOOLS_DIR="$REPO_DIR/tools"
OPENPNP_DIR="${OPENPNP_SCRIPTS_DIR:-$HOME/.openpnp2/scripts/illysky}"
BIN_DIR="$HOME/.local/bin"

echo "Repo:          $REPO_DIR"
echo "OpenPnP menu:  $OPENPNP_DIR"
echo "Local bin:     $BIN_DIR"
echo ""

# --- OpenPnP Jython scripts -------------------------------------------
mkdir -p "$OPENPNP_DIR"
for src in "$SCRIPTS_SRC"/*.py; do
    fname="$(basename "$src")"
    dest="$OPENPNP_DIR/$fname"
    [ -L "$dest" ] && rm "$dest"
    ln -s "$src" "$dest"
    echo "  linked (OpenPnP): $fname"
done

# --- pnp_creator tool -------------------------------------------------
mkdir -p "$BIN_DIR"
chmod +x "$TOOLS_DIR/pnp_creator.py"
dest="$BIN_DIR/pnp_creator"
[ -L "$dest" ] && rm "$dest"
ln -s "$TOOLS_DIR/pnp_creator.py" "$dest"
echo "  linked (bin):     pnp_creator -> $dest"

# Ensure ~/.local/bin is on PATH in .bashrc
if ! grep -q 'local/bin' "$HOME/.bashrc"; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    echo "  added ~/.local/bin to PATH in .bashrc"
fi

echo ""
echo "Done."
echo "  Run 'source ~/.bashrc' (or open a new terminal) to use pnp_creator."
echo "  Restart OpenPnP (or Scripts -> Reload) to pick up script changes."
