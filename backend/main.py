"""Launches the FastAPI server in a background thread and opens it in a native window."""
import threading

import uvicorn
import webview

from backend.api import app


def _run_server():
    uvicorn.run(app, host="127.0.0.1", port=8731, log_level="warning")


def main():
    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()
    webview.create_window("EVE Null-Sec Risk Assessor", "http://127.0.0.1:8731")
    webview.start()


if __name__ == "__main__":
    main()
