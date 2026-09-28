#!/bin/zsh
set -e
cd -- "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  echo 'Подготовка VideoDrop…'
  for candidate in "$HOME/.local/bin/python3.11" /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
      "$candidate" -m venv .venv
      break
    fi
  done
  if [[ ! -x .venv/bin/python ]]; then
    echo 'Нужен Python 3.10 или новее: https://www.python.org/downloads/macos/'
    read '?Нажмите Enter, чтобы закрыть.'
    exit 1
  fi
  .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
fi
mkdir -p .state
VIDEODROP_PORT_VALUE="$(.venv/bin/python -c 'import json; print(json.load(open("version.json")).get("port", 8765))')"
VIDEODROP_BASE="http://127.0.0.1:$VIDEODROP_PORT_VALUE"
if ! /usr/bin/curl --noproxy '*' -fsS --max-time 2 "$VIDEODROP_BASE/api/bootstrap" >/dev/null 2>&1; then
  if [[ -f .state/updates/runner.py ]]; then
    .venv/bin/python .state/updates/runner.py --recover "$PWD"
  fi
  VIDEODROP_PYTHON="$(.venv/bin/python updater.py --runtime "$PWD")"
  if ! /usr/bin/curl --noproxy '*' -fsS --max-time 2 "$VIDEODROP_BASE/api/bootstrap" >/dev/null 2>&1; then
    nohup "$VIDEODROP_PYTHON" app.py >.state/server.log 2>&1 </dev/null &!
  fi
  for attempt in {1..40}; do
    if /usr/bin/curl --noproxy '*' -fsS --max-time 1 "$VIDEODROP_BASE/api/bootstrap" >/dev/null 2>&1; then break; fi
    sleep 0.25
  done
fi
if /usr/bin/curl --noproxy '*' -fsS --max-time 2 "$VIDEODROP_BASE/api/bootstrap" >/dev/null 2>&1; then
  if [[ "${1:-}" == '--updates' ]]; then
    open -a 'Google Chrome' "$VIDEODROP_BASE/#updates"
  else
    open -a 'Google Chrome' "$VIDEODROP_BASE/"
  fi
  echo 'VideoDrop открыт в Chrome. Это окно можно закрыть.'
else
  echo 'Не удалось запустить VideoDrop. Подробности в .state/server.log'
  read '?Нажмите Enter, чтобы закрыть.'
fi
