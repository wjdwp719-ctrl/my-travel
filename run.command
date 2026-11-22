#!/bin/zsh
# 더블클릭하면 여행 기록 어드민이 열린다. 끄려면 이 터미널 창에서 Ctrl+C
cd "$(dirname "$0")"
exec python3 admin/server.py
