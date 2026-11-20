#!/usr/bin/env python3
"""Entry point: build the store, bind an ephemeral port, register, serve.

Wiring only — no forge logic lives here.  The handshake with the suite is:

1. bind ``("0.0.0.0", 0)`` and read the port the kernel chose;
2. ``POST $MAKEFUN_ENV_URL/register`` with ``{"port": <port>}`` (retried for
   about ten seconds while the suite comes up);
3. long-poll ``GET $MAKEFUN_ENV_URL/done`` on a daemon thread; when it answers
   ``{"done": true}``, stop the server and exit 0.

With ``MAKEFUN_ENV_URL`` unset (local development) the handshake is skipped and
the forge serves until interrupted.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

try:  # package import
    from forge.httpd import make_server
    from forge.store import Store
except ImportError:  # pragma: no cover - running from inside forge/
    from httpd import make_server  # type: ignore
    from store import Store  # type: ignore

__all__ = ["build_store", "register", "poll_done", "main"]

REGISTER_ATTEMPTS = 20
REGISTER_DELAY_SECONDS = 0.5
DONE_TIMEOUT_SECONDS = 30.0
DONE_ERROR_SLEEP_SECONDS = 0.2


def build_store() -> Store:
    """The one store, its logical clock seeded from MAKEFUN_TRIAL_NOW."""
    return Store(os.environ.get("MAKEFUN_TRIAL_NOW"))


def _post_json(url: str, payload: dict, timeout: float = 5.0) -> bool:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()
    return True


def register(env_url: str, port: int) -> bool:
    """Announce our port to the suite, retrying while it boots."""
    url = env_url.rstrip("/") + "/register"
    for attempt in range(REGISTER_ATTEMPTS):
        try:
            _post_json(url, {"port": port})
            return True
        except (urllib.error.URLError, OSError, ValueError):
            if attempt == REGISTER_ATTEMPTS - 1:
                return False
            time.sleep(REGISTER_DELAY_SECONDS)
    return False


def poll_done(env_url: str, server, interval: float = DONE_ERROR_SLEEP_SECONDS) -> None:
    """Long-poll ``/done``; on ``{"done": true}`` shut down and exit 0."""
    url = env_url.rstrip("/") + "/done"
    while True:
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=DONE_TIMEOUT_SECONDS) as response:
                raw = response.read()
            try:
                answer = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                answer = {}
            if isinstance(answer, dict) and answer.get("done") is True:
                break
            time.sleep(0.05)
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(interval)
            continue
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass
    sys.stdout.flush()
    os._exit(0)


def main(argv=None) -> int:
    store = build_store()
    token = os.environ.get("MAKEFUN_SHOP_TOKEN", "")
    server = make_server(store, token=token, host="0.0.0.0", port=0)
    port = server.server_address[1]

    env_url = os.environ.get("MAKEFUN_ENV_URL", "").strip()
    if env_url:
        register(env_url, port)
        thread = threading.Thread(
            target=poll_done, args=(env_url, server), name="forge-done", daemon=True
        )
        thread.start()
    else:
        sys.stdout.write("forge listening on port %d (no MAKEFUN_ENV_URL)\n" % port)
        sys.stdout.flush()

    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.server_close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
