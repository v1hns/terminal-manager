#!/usr/bin/env bash
# Install tm as a global command

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
TARGET="$BIN_DIR/tm"

mkdir -p "$BIN_DIR"
echo "Installing tm → $TARGET"
ln -sf "$SCRIPT_DIR/tm.py" "$TARGET"
chmod +x "$TARGET"

# Add ~/.local/bin to PATH if not already there
SHELL_RC="$HOME/.zshrc"
if ! grep -q '\.local/bin' "$SHELL_RC" 2>/dev/null; then
    echo '' >> "$SHELL_RC"
    echo '# local bin' >> "$SHELL_RC"
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_RC"
    echo "Added ~/.local/bin to PATH in $SHELL_RC"
    echo "Run: source ~/.zshrc"
fi

echo "Done. Run: tm"
