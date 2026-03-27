#!/usr/bin/env bash
# Install tm as a global command

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="/usr/local/bin/tm"

echo "Installing tm → $TARGET"
ln -sf "$SCRIPT_DIR/tm.py" "$TARGET"
chmod +x "$TARGET"
echo "Done. Run: tm"
