"""
run.py — Development server entry point
Usage: python3 run.py
"""
import os
from app import create_app, socketio

app = create_app()

if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
        allow_unsafe_werkzeug=True,  # required when async_mode="threading"
        use_reloader=False,          # reloader breaks threading mode
    )
