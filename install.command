#!/bin/zsh
# SPDX-License-Identifier: GPL-3.0-only
PROJECT_DIR="${0:A:h}"
/bin/zsh "$PROJECT_DIR/installers/macos.sh" install "$@"
result=$?
if [[ -t 0 && "${MAGIC_NO_PAUSE:-0}" != 1 ]]; then
  read -r '?Press Enter to close this window. '
fi
exit "$result"
