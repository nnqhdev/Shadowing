#!/bin/zsh
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then
  /usr/local/bin/python3 -m venv .venv
  ./.venv/bin/pip install -r requirements.txt
fi
(sleep 2 && open "http://127.0.0.1:8095") &
./.venv/bin/python app.py
