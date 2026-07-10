#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "ERROR: run this installer as root." >&2
  exit 2
fi

if [[ "$(uname -s)" != "Linux" ]] || ! command -v systemctl >/dev/null 2>&1; then
  echo "ERROR: this installer currently supports Linux systems using systemd." >&2
  exit 2
fi

SOURCE_DIR="${CTC_SOURCE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
INSTALL_ROOT="${CTC_INSTALL_ROOT:-/opt/ctc}"
APP_DIR="${CTC_APP_DIR:-$INSTALL_ROOT/app}"
VENV_DIR="${CTC_VENV_DIR:-$INSTALL_ROOT/.venv}"
STATE_DIR="${CTC_STATE_DIR:-/var/lib/ctc}"
CONFIG_DIR="${CTC_CONFIG_DIR:-/etc/ctc}"
ENV_SOURCE="${CTC_ENV_SOURCE:-}"
ENV_FILE="$CONFIG_DIR/ctc.env"
SERVICE_USER="${CTC_SERVICE_USER:-ctc}"
SERVICE_GROUP="${CTC_SERVICE_GROUP:-ctc}"

generate_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    "$PYTHON" -c 'import secrets; print(secrets.token_hex(32))'
  fi
}

write_env_file() {
  install -o root -g "$SERVICE_GROUP" -m 0640 /dev/null "$ENV_FILE"
  {
    printf 'CTC_UPSTREAM_BASE_URL=%q\n' "$CTC_UPSTREAM_BASE_URL"
    printf 'CTC_UPSTREAM_API_KEY=%q\n' "${CTC_UPSTREAM_API_KEY:-}"
    printf 'CTC_ADMIN_TOKEN=%q\n' "$CTC_ADMIN_TOKEN"
    if [[ -n "${CTC_LAN_PROXY_HOST:-}" && -n "${CTC_LAN_PROXY_PORT:-}" ]]; then
      printf 'CTC_LAN_PROXY_HOST=%q\n' "$CTC_LAN_PROXY_HOST"
      printf 'CTC_LAN_PROXY_PORT=%q\n' "$CTC_LAN_PROXY_PORT"
      printf 'CTC_PROXY_TOKEN=%q\n' "$CTC_PROXY_TOKEN"
    fi
  } >"$ENV_FILE"
  chown root:"$SERVICE_GROUP" "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
}

configure_first_install() {
  if [[ "${CTC_NONINTERACTIVE:-0}" == "1" ]]; then
    if [[ -z "${CTC_UPSTREAM_BASE_URL:-}" ]]; then
      echo "ERROR: CTC_UPSTREAM_BASE_URL is required for non-interactive installation." >&2
      exit 2
    fi
    CTC_ADMIN_TOKEN="${CTC_ADMIN_TOKEN:-$(generate_token)}"
    if [[ -n "${CTC_LAN_PROXY_HOST:-}" && -n "${CTC_LAN_PROXY_PORT:-}" ]]; then
      CTC_PROXY_TOKEN="${CTC_PROXY_TOKEN:-$(generate_token)}"
    fi
    write_env_file
    return
  fi

  if [[ ! -t 0 ]]; then
    echo "ERROR: no configuration file and no interactive terminal." >&2
    echo "Pass CTC_ENV_SOURCE=/path/to/ctc.env or use CTC_NONINTERACTIVE=1." >&2
    exit 2
  fi

  echo "Context Token Compressor first-time configuration"
  read -r -p "Upstream OpenAI-compatible base URL (required): " CTC_UPSTREAM_BASE_URL
  if [[ -z "$CTC_UPSTREAM_BASE_URL" ]]; then
    echo "ERROR: upstream base URL cannot be empty." >&2
    exit 2
  fi
  read -r -s -p "Upstream API key (optional): " CTC_UPSTREAM_API_KEY
  echo
  CTC_ADMIN_TOKEN="$(generate_token)"

  local enable_lan
  read -r -p "Enable authenticated LAN proxy on 0.0.0.0:8799? [y/N]: " enable_lan
  if [[ "$enable_lan" =~ ^[Yy]$ ]]; then
    CTC_LAN_PROXY_HOST="0.0.0.0"
    CTC_LAN_PROXY_PORT="8799"
    CTC_PROXY_TOKEN="$(generate_token)"
  fi

  write_env_file
  echo "Configuration written to $ENV_FILE; generated tokens were not printed."
}

PYTHON="${CTC_BOOTSTRAP_PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  PYTHON="$(command -v python3.11 || command -v python3 || true)"
fi
if [[ -z "$PYTHON" ]]; then
  echo "ERROR: Python 3.11 or newer is required." >&2
  exit 2
fi
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 2)' || {
  echo "ERROR: Python 3.11 or newer is required." >&2
  exit 2
}

if ! getent group "$SERVICE_GROUP" >/dev/null; then
  groupadd --system "$SERVICE_GROUP"
fi
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --gid "$SERVICE_GROUP" --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

install -d -o root -g root -m 0755 "$INSTALL_ROOT" "$APP_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 "$STATE_DIR"
install -d -o root -g "$SERVICE_GROUP" -m 0750 "$CONFIG_DIR"

if [[ -n "$ENV_SOURCE" ]]; then
  if [[ "$(readlink -f "$ENV_SOURCE")" != "$(readlink -f "$ENV_FILE")" ]]; then
    install -o root -g "$SERVICE_GROUP" -m 0640 "$ENV_SOURCE" "$ENV_FILE"
  fi
fi
if [[ ! -r "$ENV_FILE" ]]; then
  configure_first_install
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
if [[ -z "${CTC_UPSTREAM_BASE_URL:-}" ]]; then
  echo "ERROR: CTC_UPSTREAM_BASE_URL is required in $ENV_FILE." >&2
  exit 2
fi
if [[ -z "${CTC_ADMIN_TOKEN:-}" || "${CTC_ADMIN_TOKEN}" == generate-* ]]; then
  echo "ERROR: replace the CTC_ADMIN_TOKEN placeholder in $ENV_FILE." >&2
  exit 2
fi
if [[ -n "${CTC_LAN_PROXY_HOST:-}" && -n "${CTC_LAN_PROXY_PORT:-}" && -z "${CTC_PROXY_TOKEN:-}" ]]; then
  echo "ERROR: CTC_PROXY_TOKEN is required when the LAN listener is enabled." >&2
  exit 2
fi

CTC_SOURCE_DIR="$SOURCE_DIR" CTC_RUNTIME_DIR="$APP_DIR" "$PYTHON" "$SOURCE_DIR/scripts/sync_to_runtime.py"
"$PYTHON" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install "$APP_DIR"
"$VENV_DIR/bin/python" -m compileall "$APP_DIR/ctc" "$APP_DIR/scripts"

install -o root -g root -m 0644 "$SOURCE_DIR/deploy/ctc.service" /etc/systemd/system/ctc.service
systemctl daemon-reload
systemctl enable --now ctc.service

for _ in $(seq 1 40); do
  if curl -fsS http://127.0.0.1:8787/healthz >/dev/null && curl -fsS http://127.0.0.1:8788/healthz >/dev/null; then
    echo "Context Token Compressor installation completed; health checks passed."
    echo "Configuration: $ENV_FILE"
    echo "Dashboard: http://127.0.0.1:8788"
    exit 0
  fi
  sleep 0.5
done

echo "ERROR: CTC did not become healthy after installation." >&2
systemctl status ctc.service --no-pager >&2 || true
exit 3
