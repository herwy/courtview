"""
courtview.py - gunicorn entry point for CourtView (Padelmates API proxy server for RPi).

The application lives in the courtview_app package (see .claude/docs/courtview-internals.md,
Module map). gunicorn runs `courtview:app`; importing this module builds the Flask app and
starts the background threads unless COURTVIEW_SKIP_STARTUP=1 (tests).
"""
import os

from courtview_app import create_app, startup

app = create_app()

if os.environ.get("COURTVIEW_SKIP_STARTUP") != "1":
    startup._startup()

if __name__ == "__main__":
    # gunicorn runs courtview:app and triggers _startup() at import.
    # This block is the dev fallback (python3 courtview.py).
    app.run(host="0.0.0.0", port=8766, debug=False, threaded=True, use_reloader=False)
