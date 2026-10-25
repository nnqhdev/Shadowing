#!/bin/zsh
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv || { echo "Cần cài Python 3 (https://www.python.org)"; exit 1; }
  ./.venv/bin/pip install -r requirements.txt
fi
mkdir -p outputs
nohup ./.venv/bin/python -X utf8 desktop_app.py >>outputs/desktop.log 2>&1 &
