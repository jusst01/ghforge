"""Threaded HTTP glue: routes the four surfaces onto one server.

``ThreadingHTTPServer`` with HTTP/1.1 keep-alive; every reply carries a correct
``Content-Length`` and a non-empty ``X-GitHub-Request-Id``.  Dispatch is:

``/healthz``                    liveness probe, no auth
``/{owner}/{repo}.git/...``     git smart HTTP (info/refs, upload-pack, receive-pack)
``/api/v3`` and below           REST v3 (bearer token required)
``/api/graphql``                GraphQL v4 (bearer token required)
``/mcp``                        MCP JSON-RPC (bearer token required)
anything else                   404 JSON

A handler exception is caught and answered with 500 JSON so a malformed
request can never take the server down.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit

try:  # package imports
    from forge import gitadv, gitfetch, gitpush, graphql, mcp, rest
    from forge.store import Pull
except ImportError:  # pragma: no cover - flat import when forge/ is on sys.path
    import gitadv  # type: ignore
    import gitfetch  # type: ignore
    import gitpush  # type: ignore
    import graphql  # type: ignore
    import mcp  # type: ignore
    import rest  # type: ignore
    from store import Pull  # type: ignore

__all__ = ["ForgeServer", "ForgeHandler", "make_server", "serve_forever"]


# ---------------------------------------------------------------------------
# Pull request routes
#
# ``rest.handle_rest`` answers ``None`` for the ``pulls`` subtree, which is the
# signal that the pull request surface is served here.  The object builder and
# the squash merge live in :mod:`forge.mcp` so both surfaces share one shape.
# ---------------------------------------------------------------------------

def _json_body(body: Any) -> Any:
    return rest.parse_body(body)


def _pulls_route(
    store: Any,
    method: str,
    seg: List[str],
    query: Dict[str, str],
    body: Any,
    base_url: str,
) -> Optional[Tuple[int, Any]]:
    """Serve ``/api/v3/repos/{owner}/{repo}/pulls...``; ``None`` when unknown."""
    if len(seg) < 4 or seg[0] != "repos" or seg[3] != "pulls":
        return None
    owner, repo_name = seg[1], seg[2]
    if store.owner_login is None:
        store.bind_owner(owner)
    if store.get_owner_login() != owner:
        return 404, {"message": "Not Found"}
    repo = store.get_repo(repo_name)
    if repo is None:
        return 404, {"message": "Not Found"}

    try:
        payload = _json_body(body)
    except ValueError:
        return 400, {"message": "Malformed JSON"}
    if not isinstance(payload, dict):
        payload = {}

    sub = seg[4:]

    if not sub:
        if method == "GET":
            wanted = query.get("state", "open")
            with store.lock:
                pulls = sorted(repo.pulls.values(), key=lambda p: p.number, reverse=True)
                return 200, [
                    mcp.make_pull_obj(store, repo, pull, base_url)
                    for pull in pulls
                    if wanted in ("all", "any") or pull.state == wanted
                ]
        if method == "POST":
            title = payload.get("title")
            if not isinstance(title, str) or not title:
                return 422, {"message": "Title is required"}
            head = payload.get("head")
            base = payload.get("base") or repo.default_branch
            if not isinstance(head, str) or not head:
                return 422, {"message": "Head is required"}
            if not isinstance(base, str) or not base:
                return 422, {"message": "Base is required"}
            if ":" in head:
                head = head.split(":")[-1]
            if ":" in base:
                base = base.split(":")[-1]
            head_sha = repo.get_ref("refs/heads/%s" % head)
            base_sha = repo.get_ref("refs/heads/%s" % base)
            if not head_sha:
                return 422, {"message": "Head sha can't be blank"}
            if not base_sha:
                return 422, {"message": "Base sha can't be blank"}
            if head == base:
                return 422, {"message": "No commits between %s and %s" % (base, head)}
            pull_body = payload.get("body")
            with store.lock:
                for existing in repo.pulls.values():
                    if (
                        existing.state == "open"
                        and existing.head_ref == head
                        and existing.base_ref == base
                    ):
                        return 422, {
                            "message": "A pull request already exists for %s:%s."
                            % (store.get_owner_login(), head)
                        }
                number = repo.next_number
                repo.next_number += 1
                db_id = store.next_pull_db_id
                store.next_pull_db_id += 1
                now = store.now_iso()
                pull = Pull(
                    number=number,
                    title=title,
                    body=pull_body if isinstance(pull_body, str) else "",
                    state="open",
                    head_ref=head,
                    base_ref=base,
                    head_sha=head_sha,
                    base_sha=base_sha,
                    merged=False,
                    author=store.get_owner_login(),
                    created_at=now,
                    updated_at=now,
                    db_id=db_id,
                )
                repo.pulls[number] = pull
            return 201, mcp.make_pull_obj(store, repo, pull, base_url)
        return 404, {"message": "Not Found"}

    try:
        number = int(sub[0])
    except ValueError:
        return 404, {"message": "Not Found"}
    pull = repo.get_pull(number)
    if pull is None:
        return 404, {"message": "Not Found"}
    tail = sub[1:]

    if not tail:
        if method == "GET":
            return 200, mcp.make_pull_obj(store, repo, pull, base_url)
        if method == "PATCH":
            if isinstance(payload.get("title"), str):
                pull.title = payload["title"]
            if isinstance(payload.get("body"), str):
                pull.body = payload["body"]
            if payload.get("state") in ("open", "closed"):
                pull.state = payload["state"]
            pull.updated_at = store.now_iso()
            return 200, mcp.make_pull_obj(store, repo, pull, base_url)
        return 404, {"message": "Not Found"}

    if tail == ["merge"]:
        if method == "GET":
            if pull.merged:
                return 204, None
            return 404, {"message": "Not Found"}
        if method == "PUT":
            try:
                sha = mcp.merge_pull(store, repo, pull)
            except mcp.ToolError:
                if pull.merged:
                    return 200, {
                        "sha": pull.merge_commit_sha,
                        "merged": True,
                        "message": "Pull Request successfully merged",
                    }
                return 405, {"message": "Pull Request is not mergeable"}
            return 200, {
                "sha": sha,
                "merged": True,
                "message": "Pull Request successfully merged",
            }
        return 404, {"message": "Not Found"}

    if tail == ["files"] and method == "GET":
        return 200, []

    if tail == ["commits"] and method == "GET":
        return 200, []

    if tail == ["requested_reviewers"] and method == "GET":
        return 200, {"users": [], "teams": []}

    return 404, {"message": "Not Found"}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class ForgeHandler(BaseHTTPRequestHandler):
    """One request; every dispatch path answers JSON (git paths aside)."""

    protocol_version = "HTTP/1.1"
    server_version = "forge/1.0"
    sys_version = ""

    # -- plumbing ---------------------------------------------------------

    def send_error(self, code, message=None, explain=None):
        if self.path.startswith("/api/v3"):
            self._send_json(code, {"message": message or "Error"})
        else:
            super().send_error(code, message, explain)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence the default stderr access log."""

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence the default stderr error log."""

    @property
    def forge(self) -> Any:
        return self.server  # type: ignore[attr-defined]

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length) or b""

    def _base_url(self) -> str:
        host = self.headers.get("Host") or "localhost"
        return "http://%s" % host

    def _send(
        self,
        status: int,
        payload: Any = None,
        content_type: str = "application/json; charset=utf-8",
        extra_headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
    ) -> None:
        if body is None:
            if payload is None:
                body = b""
            else:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-GitHub-Request-Id", self.forge.next_request_id())
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, status: int, payload: Any, extra: Optional[Dict[str, str]] = None) -> None:
        self._send(status, payload=payload, extra_headers=extra)

    # -- verbs ------------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        self._dispatch("OPTIONS")

    # -- dispatch ---------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        body = self._read_body()
        try:
            self._route(method, body)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            try:
                self._send_json(500, {"message": "internal error"})
            except Exception:
                pass

    def _route(self, method: str, body: bytes) -> None:
        split = urlsplit(self.path)
        path = split.path or "/"
        query_string = split.query or ""
        seg = [part for part in path.strip("/").split("/") if part]

        if path == "/healthz" or path == "/healthz/":
            if method in ("GET", "HEAD"):
                return self._send_json(200, {"ok": True})
            return self._send_json(404, {"message": "Not Found"})

        if seg[:1] == ["mcp"] and len(seg) == 1:
            if method != "POST":
                return self._send_json(405, {"message": "Method Not Allowed"})
            status, payload = mcp.handle_mcp(
                self.forge.store,
                body,
                authorization=self.headers.get("Authorization"),
                token=self.forge.token,
                base_url=self._base_url(),
            )
            return self._send_json(status, payload)

        if seg[:2] == ["api", "graphql"]:
            if method != "POST":
                return self._send_json(404, {"message": "Not Found"})
            if not self.forge.authorized(self.headers.get("Authorization")):
                return self._send_json(401, {"message": "Bad credentials"})
            status, payload = graphql.handle_graphql(
                self.forge.store, body, self._base_url()
            )
            return self._send_json(status, payload)

        if seg[:2] == ["api", "v3"] or seg[:1] == ["api"] and seg[1:2] == ["v3"]:
            if not self.forge.authorized(self.headers.get("Authorization")):
                return self._send_json(401, {"message": "Bad credentials"})
            return self._rest(method, seg, query_string, body)

        if len(seg) >= 2 and seg[1].endswith(".git"):
            return self._git(method, seg, query_string, body)

        return self._send_json(404, {"message": "Not Found"})

    def _rest(self, method: str, seg: List[str], query_string: str, body: bytes) -> None:
        parsed = parse_qs(query_string or "")
        query = {key: values[0] for key, values in parsed.items() if values}
        base_url = self._base_url()
        result = rest.handle_rest(self.forge.store, method, seg, query, body, base_url)
        if result is None:
            sub = seg[2:] if seg[:2] == ["api", "v3"] else seg
            result = _pulls_route(self.forge.store, method, sub, query, body, base_url)
        if result is None:
            return self._send_json(404, {"message": "Not Found"})
        status, payload = result
        extra = {"X-OAuth-Scopes": "repo, user"}
        if method == "GET" and status == 200 and isinstance(payload, list) and "per_page" in query:
            try:
                per_page = int(query["per_page"])
                if len(payload) == per_page:
                    current_page = int(query.get("page", 1))
                    next_page = current_page + 1
                    next_query = query.copy()
                    next_query["page"] = str(next_page)
                    split = urlsplit(self.path)
                    next_url = f"{base_url}{split.path}?{urlencode(next_query)}"
                    extra["Link"] = f"<{next_url}>; rel=\"next\""
            except ValueError:
                pass
        if payload is None and status in (204, 304):
            return self._send(
                status,
                payload=None,
                extra_headers=extra,
                body=b"",
            )
        return self._send_json(
            status, payload, extra=extra
        )

    def _git(self, method: str, seg: List[str], query_string: str, body: bytes) -> None:
        owner = seg[0]
        repo_name = seg[1][: -len(".git")]
        tail = seg[2:]
        service = ""
        if query_string:
            values = parse_qs(query_string).get("service")
            if values:
                service = values[0]
        git_protocol = self.headers.get("Git-Protocol")

        if tail == ["info", "refs"]:
            if method not in ("GET", "HEAD"):
                return self._send_git(405, "text/plain; charset=utf-8", b"method not allowed\n")
            status, content_type, out = gitadv.handle_info_refs(
                self.forge.store, owner, repo_name, service, git_protocol
            )
            return self._send_git(status, content_type, out)

        if tail == ["git-upload-pack"]:
            if method != "POST":
                return self._send_git(405, "text/plain; charset=utf-8", b"method not allowed\n")
            if _is_ls_refs(body):
                status, content_type, out = gitadv.handle_ls_refs(
                    self.forge.store, owner, repo_name, body
                )
            else:
                status, content_type, out = gitfetch.handle_upload_pack(
                    self.forge.store, owner, repo_name, body, git_protocol
                )
            return self._send_git(status, content_type, out)

        if tail == ["git-receive-pack"]:
            if method != "POST":
                return self._send_git(405, "text/plain; charset=utf-8", b"method not allowed\n")
            status, content_type, out = gitpush.handle_receive_pack(
                self.forge.store, owner, repo_name, body
            )
            return self._send_git(status, content_type, out)

        return self._send_git(404, "text/plain; charset=utf-8", b"not found\n")

    def _send_git(self, status: int, content_type: str, body: bytes) -> None:
        headers = {}
        if content_type and "text/" not in content_type:
            headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
        self._send(status, content_type=content_type, extra_headers=headers, body=body)


def _is_ls_refs(body: bytes) -> bool:
    """Whether a v2 body opens with ``command=ls-refs``."""
    if not body or len(body) < 4:
        return False
    try:
        length = int(body[:4].decode("ascii"), 16)
    except (ValueError, UnicodeDecodeError):
        return False
    if length < 5:
        return False
    head = body[4:min(length, 64)]
    return head.startswith(b"command=ls-refs")


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class ForgeServer(ThreadingHTTPServer):
    """The forge's server: carries the store, the token and the request counter."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], store: Any, token: str = "") -> None:
        self.store = store
        self.token = token or os.environ.get("MAKEFUN_SHOP_TOKEN", "")
        self._request_lock = threading.Lock()
        self._request_counter = 0
        super().__init__(address, ForgeHandler)

    def next_request_id(self) -> str:
        with self._request_lock:
            self._request_counter += 1
            return "%d" % self._request_counter

    def authorized(self, authorization: Optional[str]) -> bool:
        if not self.token:
            return True
        if not isinstance(authorization, str):
            return False
        scheme, _, presented = authorization.partition(" ")
        return scheme.lower() == "bearer" and presented.strip() == self.token


def make_server(store: Any, token: str = "", host: str = "0.0.0.0", port: int = 0) -> ForgeServer:
    """Bind ``(host, port)`` and answer the ready server (port 0 = kernel picks)."""
    return ForgeServer((host, port), store, token)


def serve_forever(server: ForgeServer) -> None:
    server.serve_forever(poll_interval=0.1)
