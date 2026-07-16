"""`python -m web` — start the desk app without remembering uvicorn syntax.

Uses PORT from the environment (default 8000); if that port is taken,
walks forward to the next free one instead of dying with WinError 10013.
"""

import os
import socket

import uvicorn


def free_port(start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise SystemExit(f"no free port found in {start}-{start + 19}")


if __name__ == "__main__":
    port = free_port(int(os.getenv("PORT", "8000")))
    print(f"AI Trading Desk -> http://localhost:{port}")
    uvicorn.run("web.server:app", host="127.0.0.1", port=port, reload=True)
