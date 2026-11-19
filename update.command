#!/bin/zsh
# SPDX-License-Identifier: GPL-3.0-only
UPDATER_DIR="${0:A:h}"
UPDATE_TARGET="$UPDATER_DIR"
if [[ ! -f "$UPDATE_TARGET/main.py" ]]; then
  UPDATE_TARGET=$(/usr/bin/osascript -e 'POSIX path of (choose folder with prompt "기존 Magic-Instrumental 폴더를 선택하세요. targets 폴더가 들어 있는 위치입니다.")') || exit 1
fi
VENV_PYTHON="$HOME/.vocal-separator-and-speed-adj/.venv/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
  print '기존 프로그램의 install.command를 먼저 실행한 뒤 다시 업데이트하세요.'
  [[ -t 0 ]] && read -r '?Press Enter to close. '
  exit 1
fi
export PYTHONUTF8=1
exec "$VENV_PYTHON" "$UPDATER_DIR/update.py" --project "$UPDATE_TARGET" --pause --wait-for-app
