#!/bin/zsh
# watchdog.sh - restart gunicorn courtview if it dies. Run in background on the RPi.
# Loops every 60s. Logs restarts to /root/projects/courtview/watchdog.log.

LOG=/root/projects/courtview/watchdog.log
CV_DIR=/root/projects/courtview

ts() { date '+%Y-%m-%dT%H:%M:%S'; }

while true; do
    if ! pgrep -f "gunicorn.*courtview|courtview.py" >/dev/null 2>&1; then
        echo "[$(ts)] courtview not running - restarting via gunicorn" >> "$LOG"
        cd "$CV_DIR" && nohup gunicorn --workers 1 --worker-class gthread --threads 4 --bind 127.0.0.1:8766 --timeout 60 --log-level warning courtview:app >> "$CV_DIR/courtview.log" 2>&1 &
    fi
    sleep 60
done
