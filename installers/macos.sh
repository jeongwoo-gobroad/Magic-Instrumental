#!/bin/zsh
# SPDX-License-Identifier: GPL-3.0-only
set -euo pipefail
PROJECT_DIR="${0:A:h:h}"
cd -- "$PROJECT_DIR"
mode="${1:-install}"
shift
if [[ "$mode" != install && "$mode" != launch ]]; then
  print -u2 'Unknown mode. Use install or launch.'
  exit 1
fi
if [[ "$(/usr/bin/uname -m)" != arm64 ]]; then
  print -u2 'Magic-Instrumental requires an Apple Silicon Mac. Open Terminal without Rosetta.'
  exit 1
fi
if (( ${$(/usr/bin/sw_vers -productVersion)%%.*} < 14 )); then
  print -u2 'Magic-Instrumental requires macOS 14 or newer.'
  exit 1
fi
# Retain the existing external environment location for upgrade compatibility.
VENV_PYTHON="$HOME/.vocal-separator-and-speed-adj/.venv/bin/python"
RUNTIME_DIR="$HOME/.vocal-separator-and-speed-adj/runtime"
export PYTHONUTF8=1
export UV_PYTHON_INSTALL_DIR="$RUNTIME_DIR/python"
export UV_CACHE_DIR="$RUNTIME_DIR/cache"
export UV_NO_CONFIG=1

install_app() {
  local uv_version archive expected python_version uv_path temp_dir actual managed_python
  uv_version=$(/usr/bin/plutil -extract uv.version raw -o - installers/runtime.json)
  archive=$(/usr/bin/plutil -extract uv.macos_arm64.archive raw -o - installers/runtime.json)
  expected=$(/usr/bin/plutil -extract uv.macos_arm64.sha256 raw -o - installers/runtime.json)
  python_version=$(/usr/bin/plutil -extract python raw -o - installers/runtime.json)
  uv_path="$RUNTIME_DIR/uv-$uv_version/uv"
  if [[ ! -x "$uv_path" ]]; then
    mkdir -p -- "$RUNTIME_DIR"
    temp_dir=$(mktemp -d "$RUNTIME_DIR/download.XXXXXXXX")
    print 'Downloading the verified Python installer (uv)...'
    if ! /usr/bin/curl --proto '=https' --tlsv1.2 --fail --location --retry 3 \
      --connect-timeout 30 --max-time 600 \
      "https://github.com/astral-sh/uv/releases/download/$uv_version/$archive" \
      --output "$temp_dir/archive.tar.gz"; then
      rm -rf -- "$temp_dir"
      return 1
    fi
    actual=$(/usr/bin/shasum -a 256 "$temp_dir/archive.tar.gz")
    if [[ "${actual%% *}" != "$expected" ]]; then
      print -u2 'Download checksum mismatch. Nothing was executed. Please retry installation.'
      rm -rf -- "$temp_dir"
      return 1
    fi
    /usr/bin/tar -xzf "$temp_dir/archive.tar.gz" -C "$temp_dir"
    mkdir -p -- "${uv_path:h}"
    /bin/cp "$temp_dir/uv-aarch64-apple-darwin/uv" "$uv_path"
    /bin/chmod 755 "$uv_path"
    rm -rf -- "$temp_dir"
  fi
  print "Installing private Python $python_version (no global Python changes)..."
  "$uv_path" python install "$python_version" --no-bin --no-registry
  managed_python=$("$uv_path" python find "$python_version" --managed-python \
    --no-project --no-python-downloads)
  "$managed_python" "$PROJECT_DIR/bootstrap.py" --setup-only "$@"
}

if [[ "$mode" == install ]]; then
  install_app "$@"
elif [[ ! -x "$VENV_PYTHON" ]]; then
  install_app "$@"
  "$VENV_PYTHON" "$PROJECT_DIR/bootstrap.py" --launch-only
else
  "$VENV_PYTHON" "$PROJECT_DIR/bootstrap.py" --launch-only "$@"
fi
