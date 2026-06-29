"""
MonitorPi — run.py
Process manager: starts uvicorn server, waits for readiness, then starts
client. Auto-restarts client on crash. Handles SIGINT/SIGTERM cleanly.
"""

import sys
import time
import signal
import subprocess
import requests

SERVER_URL   = "http://127.0.0.1:8000/api/mode"
POLL_RETRIES = 30
POLL_INTERVAL = 1        # seconds
CLIENT_RESTART_DELAY = 3 # seconds

_server_proc: subprocess.Popen | None = None
_client_proc: subprocess.Popen | None = None
_shutdown = False


def _terminate(proc: subprocess.Popen | None, name: str):
    if proc is None:
        return
    if proc.poll() is None:
        print(f"[run] Terminating {name} (pid={proc.pid})…")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print(f"[run] {name} did not stop — killing")
            proc.kill()
            proc.wait()
    print(f"[run] {name} exited with code {proc.returncode}")


def _signal_handler(signum, frame):
    global _shutdown
    print(f"\n[run] Signal {signum} received — shutting down")
    _shutdown = True
    _terminate(_client_proc, "client")
    _terminate(_server_proc, "server")
    sys.exit(0)


signal.signal(signal.SIGINT,  _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def start_server() -> subprocess.Popen:
    cmd = [
        sys.executable, "-m", "uvicorn", "server:app",
        "--host", "0.0.0.0",
        "--port", "8000",
        "--log-level", "info",
    ]
    print(f"[run] Starting server: {' '.join(cmd)}")
    return subprocess.Popen(cmd)


def wait_for_server() -> bool:
    print(f"[run] Waiting for server at {SERVER_URL} …")
    for i in range(POLL_RETRIES):
        try:
            r = requests.get(SERVER_URL, timeout=2)
            if r.status_code == 200:
                print(f"[run] Server ready after {i+1} attempt(s)")
                return True
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)
    print("[run] Server did not become ready in time")
    return False


def start_client() -> subprocess.Popen:
    cmd = [sys.executable, "client.py"]
    print(f"[run] Starting client: {' '.join(cmd)}")
    return subprocess.Popen(cmd)


def main():
    global _server_proc, _client_proc, _shutdown

    _server_proc = start_server()

    if not wait_for_server():
        print("[run] Aborting — server never became ready")
        _terminate(_server_proc, "server")
        sys.exit(1)

    _client_proc = start_client()

    try:
        while not _shutdown:
            # Check server
            ret = _server_proc.poll()
            if ret is not None:
                print(f"[run] Server exited (code={ret}) — stopping everything")
                _terminate(_client_proc, "client")
                sys.exit(ret)

            # Check client
            ret = _client_proc.poll()
            if ret is not None:
                if _shutdown:
                    break
                print(f"[run] Client exited (code={ret}) — restarting in {CLIENT_RESTART_DELAY}s")
                time.sleep(CLIENT_RESTART_DELAY)
                if not _shutdown:
                    _client_proc = start_client()

            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        _terminate(_client_proc, "client")
        _terminate(_server_proc, "server")


if __name__ == "__main__":
    main()
