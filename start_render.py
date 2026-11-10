from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
PROVIDER = ROOT / ".runtime" / "bgutil-ytdlp-pot-provider" / "server"
POT_URL = "http://127.0.0.1:4416"


def main():
    children = []

    def shutdown(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        provider = subprocess.Popen(
            ["node", "build/main.js", "--host", "127.0.0.1", "--port", "4416"],
            cwd=PROVIDER,
        )
        children.append(provider)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if provider.poll() is not None:
                raise SystemExit("Token provider exited before startup. Check its startup error above.")
            try:
                response = requests.get(POT_URL + "/ping", timeout=1)
                response.raise_for_status()
                version = response.json().get("version")
                if version != "2.0.2":
                    raise SystemExit(f"Token provider version mismatch: expected 2.0.2, got {version!r}.")
                break
            except (requests.RequestException, ValueError):
                time.sleep(0.25)
        else:
            raise SystemExit("Token provider did not become ready within 60 seconds.")

        print(f"Pulse token provider ready: {version} (private loopback listener)", flush=True)
        env = {**os.environ, "PULSE_POT_URL": POT_URL}
        children.append(subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env))
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
        raise SystemExit(next(child.returncode or 1 for child in children if child.poll() is not None))
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    main()
