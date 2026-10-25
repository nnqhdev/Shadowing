#!/bin/zsh
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv || { echo "Cần cài Python 3 (https://www.python.org)"; exit 1; }
  ./.venv/bin/pip install -r requirements.txt
fi
(sleep 2 && open "http://127.0.0.1:8095") &
./.venv/bin/python -X utf8 app.py
