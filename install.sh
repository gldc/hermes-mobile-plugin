#!/usr/bin/env bash
# Install hermes-mobile by symlinking this repo into ~/.hermes/plugins/.
#
# Idempotent: re-running refreshes the symlink. Refuses to clobber a
# real directory/file at the target (only replaces a symlink it could
# have created itself).
set -euo pipefail

PLUGIN_NAME="hermes-mobile"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGINS_DIR="$HERMES_HOME/plugins"
TARGET="$PLUGINS_DIR/$PLUGIN_NAME"

mkdir -p "$PLUGINS_DIR"

if [ -L "$TARGET" ]; then
    current="$(readlink "$TARGET")"
    if [ "$current" = "$REPO_DIR" ]; then
        echo "Already installed: $TARGET -> $REPO_DIR"
    else
        echo "Updating symlink: $TARGET (was -> $current)"
        ln -sfn "$REPO_DIR" "$TARGET"
    fi
elif [ -e "$TARGET" ]; then
    echo "ERROR: $TARGET exists and is not a symlink — refusing to clobber it." >&2
    echo "Move it aside first if you really want to replace it." >&2
    exit 1
else
    ln -s "$REPO_DIR" "$TARGET"
    echo "Installed: $TARGET -> $REPO_DIR"
fi

cat <<EOF

Next steps:
  1. Enable the plugin (user plugins are opt-in):
       hermes plugins enable $PLUGIN_NAME
  2. Pair your phone:
       hermes mobile pair --name "my-iphone"

To uninstall:
  rm "$TARGET"
EOF
