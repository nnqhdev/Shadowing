#!/bin/zsh
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then
  /usr/local/bin/python3 -m venv .venv
  ./.venv/bin/pip install -r requirements.txt
fi
nohup ./.venv/bin/python -X utf8 desktop_app.py >/dev/null 2>&1 &
