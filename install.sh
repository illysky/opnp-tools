#!/usr/bin/env bash
# install.sh — symlink scripts into OpenPnP's Scripts menu
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS_SRC="$REPO_DIR/scripts"
OPENPNP_DIR="${OPENPNP_SCRIPTS_DIR:-$HOME/.openpnp2/scripts/illysky}"

echo "Installing OpenPnP scripts from: $SCRIPTS_SRC"
echo "Installing into:                 $OPENPNP_DIR"
echo ""

mkdir -p "$OPENPNP_DIR"

for src in "$SCRIPTS_SRC"/*.py; do
    fname="$(basename "$src")"
    dest="$OPENPNP_DIR/$fname"
    if [ -L "$dest" ]; then
        rm "$dest"
    fi
    ln -s "$src" "$dest"
    echo "  linked: $fname"
done

echo ""
echo "Done. Restart OpenPnP (or Scripts -> Reload Scripts) to pick up changes."
