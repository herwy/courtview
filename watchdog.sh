#!/bin/zsh
# watchdog.sh - restart gunicorn courtview if it dies. Run in background on the RPi.
# Loops every 60s. Logs restarts to /root/projects/courtview/watchdog.log.

LOG=/root/projects/courtview/watchdog.log
CV_DIR=/root/projects/courtview

ts() { date '+%Y-%m-%dT%H:%M:%S'; }

TLS_FLAGS=""
if [ -f /root/projects/courtview/certs/labs.doxx.crt ] && [ -f /root/projects/courtview/certs/labs.doxx.key ]; then
    TLS_FLAGS="--certfile /root/projects/courtview/certs/labs.doxx.crt --keyfile /root/projects/courtview/certs/labs.doxx.key"
else
    echo "[$(ts)] WARNING: cert files not found, starting without TLS" >> "$LOG"
fi

while true; do
    if ! pgrep -f "gunicorn.*courtview|courtview.py" >/dev/null 2>&1; then
        echo "[$(ts)] courtview not running - restarting via gunicorn" >> "$LOG"
        cd "$CV_DIR" && nohup gunicorn --workers 1 --worker-class gthread --threads 4 --bind 0.0.0.0:8766 --timeout 60 --log-level warning $TLS_FLAGS courtview:app >> "$CV_DIR/courtview.log" 2>&1 &
    fi
    sleep 60
done
