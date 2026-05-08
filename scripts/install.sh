#!/usr/bin/env bash
# Install / enable the Hermes Sentinel plugin.
#
# Strategy: if Hermes is on the path, use `hermes plugins install` against
# the local directory. Otherwise drop a symlink into ~/.hermes/plugins/ and
# print instructions.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PLUGIN_NAME="sentinel"

echo "Installing Hermes Sentinel from ${PLUGIN_DIR}"

if command -v hermes >/dev/null; then
  hermes plugins install "${PLUGIN_DIR}" || true
  hermes plugins enable "${PLUGIN_NAME}" || true
  hermes plugins list
  exit 0
fi

# No hermes CLI: best-effort symlink + manual config snippet.
TARGET="${HOME}/.hermes/plugins/${PLUGIN_NAME}"
mkdir -p "$(dirname "${TARGET}")"
if [[ ! -e "${TARGET}" ]]; then
  ln -s "${PLUGIN_DIR}" "${TARGET}"
  echo "Symlinked ${TARGET} -> ${PLUGIN_DIR}"
fi

cat <<'EOF'

Hermes CLI was not found on PATH. To finish enabling the plugin, add this
to ~/.hermes/config.yaml:

    plugins:
      enabled:
        - sentinel

Then run `hermes plugins list` to verify it loads.
EOF
