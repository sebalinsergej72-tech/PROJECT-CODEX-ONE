#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON_BIN="${SMARTBRAIN_BOOTSTRAP_PYTHON:-python3}"
INSTALL_DEPS="${SMARTBRAIN_BOOTSTRAP_INSTALL_DEPS:-false}"
ENV_FILE="${SMARTBRAIN_OPS_ENV_FILE:-}"

if [[ -n "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
fi

if [[ "$INSTALL_DEPS" == "true" || "$INSTALL_DEPS" == "1" ]]; then
  VENV_DIR="$ROOT_DIR/.venv-ops"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip
  "$VENV_DIR/bin/pip" install -r "$ROOT_DIR/scripts/ops/requirements.txt"
  exec "$VENV_DIR/bin/python" "$ROOT_DIR/scripts/ops/bootstrap_smartbrain.py"
fi

exec "$PYTHON_BIN" "$ROOT_DIR/scripts/ops/bootstrap_smartbrain.py"
