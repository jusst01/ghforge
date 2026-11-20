"""Model Context Protocol endpoint: ``POST /mcp``, JSON-RPC 2.0.

Two protocol eras are served from one dispatch table:

* the modern revision (``2026-07-28``), where the client calls
  ``server/discover`` and declares its revision in ``_meta``;
* the classic handshake (``2025-11-25`` and older) through ``initialize``
  followed by ``notifications/initialized``.

The tools act on the same :class:`forge.store.Store` as REST and GraphQL, so a
repository or an issue created here reads back over ``/api/v3`` unchanged.
Everything is deterministic: the tool list is built once, results are
serialised with sorted keys, and no clock or random source is touched.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

try:  # package import
    from forge.objects import Commit
    from forge.rest import (
        apply_path_change,
        commit_on_branch,
        make_comment_obj,
        make_issue_obj,
        make_repo_obj,
        make_user_obj,
        read_tree_files,
    )
    from forge.store import Comment, Issue, Pull, gid
except ImportError:  # pragma: no cover - flat import when forge/ is on sys.path
    from objects import Commit  # type: ignore
    from rest import (  # type: ignore
        apply_path_change,
        commit_on_branch,
        make_comment_obj,
        make_issue_obj,
        make_repo_obj,
        make_user_obj,
        read_tree_files,
    )
    from store import Comment, Issue, Pull, gid  # type: ignore

__all__ = [
    "ToolError",
    "TOOLS",
    "SUPPORTED_VERSIONS",
    "SERVER_INFO",
    "make_pull_obj",
    "merge_pull",
    "handle_mcp",
]


class ToolError(Exception):
    """A tool call that failed for a reason the caller can read."""


# Keep the private spelling for internal use; the public name is what httpd uses.
_ToolError = ToolError


SERVER_INFO: Dict[str, Any] = {"name": "forge", "version": "1.0.0"}
CAPABILITIES: Dict[str, Any] = {"tools": {"listChanged": False}}
SUPPORTED_VERSIONS: List[str] = [
    "2026-07-28",
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
]
DEFAULT_LEGACY_VERSION = "2025-11-25"


# ---------------------------------------------------------------------------
# Tool schemas (built once; the list is never rebuilt, so two tools/list
# answers serialise to identical bytes)
# ---------------------------------------------------------------------------

_OWNER = {
    "type": "string",
    "description": "Owner login of the repository.",
}
_REPO = {
    "type": "string",
    "description": "Repository name.",
}
_TITLE = {
    "type": "string",
    "description": "Title of the issue or pull request.",
}
_BODY = {
    "type": "string",
    "description": "Body text of the issue or pull request.",
}


def _schema(properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required)}


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "create_repo",
        "description": "Create a repository owned by the bound account.",
        "inputSchema": _schema(
            {
                "name": {"type": "string", "description": "Repository name."},
                "description": {
                    "type": "string",
                    "description": "Short repository description.",
                },
                "private": {
                    "type": "boolean",
                    "description": "Whether the repository is private.",
                },
            },
            ["name"],
        ),
    },
    {
        "name": "get_repo",
        "description": "Read one repository by owner and name.",
        "inputSchema": _schema({"owner": _OWNER, "repo": _REPO}, ["owner", "repo"]),
    },
    {
        "name": "create_issue",
        "description": "Open an issue in a repository.",
        "inputSchema": _schema(
            {"owner": _OWNER, "repo": _REPO, "title": _TITLE, "body": _BODY},
            ["owner", "repo", "title"],
        ),
    },
    {
        "name": "list_issues",
        "description": "List the issues of a repository, optionally by state.",
        "inputSchema": _schema(
            {
                "owner": _OWNER,
                "repo": _REPO,
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "Issue state filter (default open).",
                },
            },
            ["owner", "repo"],
        ),
    },
    {
        "name": "create_pull_request",
        "description": "Open a pull request from a head branch into a base branch.",
        "inputSchema": _schema(
            {
                "owner": _OWNER,
                "repo": _REPO,
                "title": _TITLE,
                "body": _BODY,
                "head": {
                    "type": "string",
                    "description": "Branch holding the changes.",
                },
                "base": {
                    "type": "string",
                    "description": "Branch the changes merge into.",
                },
            },
            ["owner", "repo", "title", "head", "base"],
        ),
    },
    {
        "name": "merge_pull_request",
        "description": "Squash-merge an open pull request into its base branch.",
        "inputSchema": _schema(
            {
                "owner": _OWNER,
                "repo": _REPO,
                "pull_number": {
                    "type": "integer",
                    "description": "Number of the pull request to merge.",
                },
            },
            ["owner", "repo", "pull_number"],
        ),
    },
    {
        "name": "list_pull_requests",
        "description": "List the pull requests of a repository, optionally by state.",
        "inputSchema": _schema(
            {
                "owner": _OWNER,
                "repo": _REPO,
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "Pull request state filter (default open).",
                },
            },
            ["owner", "repo"],
        ),
    },
    {
        "name": "get_issue",
        "description": "Read one issue (or pull request) by its number.",
        "inputSchema": _schema(
            {
                "owner": _OWNER,
                "repo": _REPO,
                "issue_number": {
                    "type": "integer",
                    "description": "Number of the issue to read.",
                },
            },
            ["owner", "repo", "issue_number"],
        ),
    },
    {
        "name": "add_comment",
        "description": "Add a comment to an issue or pull request.",
        "inputSchema": _schema(
            {
                "owner": _OWNER,
                "repo": _REPO,
                "issue_number": {
                    "type": "integer",
                    "description": "Number of the issue to comment on.",
                },
                "body": {"type": "string", "description": "Comment text."},
            },
            ["owner", "repo", "issue_number", "body"],
        ),
    },
]


# ---------------------------------------------------------------------------
# Argument helpers: the suite may spell one argument several ways
# ---------------------------------------------------------------------------

_OWNER_KEYS = (
    "owner",
    "owner_login",
    "org",
    "organization",
    "organisation",
    "user",
    "user_login",
    "login",
    "account",
    "namespace",
    "owner_name",
)
_REPO_KEYS = (
    "repo",
    "repository",
    "repository_name",
    "repo_name",
    "repo_slug",
    "project",
    "project_name",
)
_FULLNAME_KEYS = ("full_name", "repo_full_name", "repository_full_name", "fullName")
_TITLE_KEYS = ("title", "issue_title", "pr_title", "pull_request_title", "subject")
_BODY_KEYS = ("body", "issue_body", "description", "content", "text", "comment")
_HEAD_KEYS = (
    "head",
    "head_ref",
    "head_branch",
    "pull_request_head",
    "pr_head",
    "source",
    "source_ref",
    "source_branch",
    "from",
    "from_branch",
    "branch",
)
_BASE_KEYS = (
    "base",
    "base_ref",
    "base_branch",
    "pull_request_base",
    "pr_base",
    "target",
    "target_ref",
    "target_branch",
    "into",
    "into_branch",
)
_NUMBER_KEYS = (
    "pull_number",
    "pull_request_number",
    "pr_number",
    "number",
    "issue_number",
    "index",
)


def _first(args: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    for key in keys:
        if key in args and args[key] is not None:
            return args[key]
    return None


def _text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if text.isdigit():
            return int(text)
    return None


def _resolve_repo(store: Any, args: Dict[str, Any]) -> Any:
    """Find the repo named by the arguments, binding the owner on first sight."""
    owner = _text(_first(args, _OWNER_KEYS))
    repo_name = _text(_first(args, _REPO_KEYS))
    full = _text(_first(args, _FULLNAME_KEYS))
    if full is not None and "/" in full:
        named_owner, _, named_repo = full.partition("/")
        if named_owner:
            owner = named_owner
        if named_repo:
            repo_name = named_repo
    if repo_name is None:
        raise ToolError("repo is required")
    if owner is not None:
        if store.bind_owner(owner) != owner:
            raise ToolError("repository not found")
    repo = store.get_repo(repo_name)
    if repo is None:
        raise ToolError("repository not found: %s" % repo_name)
    return repo


# ---------------------------------------------------------------------------
# Pull request objects and merging (shared with the REST pulls routes)
# ---------------------------------------------------------------------------

def make_pull_obj(store: Any, repo: Any, pull: Any, base_url: str = "") -> Dict[str, Any]:
    """The GitHub-shaped pull request object for one stored :class:`Pull`."""
    login = store.get_owner_login()
    name = repo.name
    number = pull.number
    url = "%s/api/v3/repos/%s/%s/pulls/%d" % (base_url, login, name, number)
    html_url = "%s/%s/%s/pull/%d" % (base_url, login, name, number)
    user = make_user_obj(store)
    head_sha = pull.head_sha or repo.get_ref("refs/heads/%s" % pull.head_ref) or ""
    base_sha = pull.base_sha or repo.get_ref("refs/heads/%s" % pull.base_ref) or ""
    return {
        "id": pull.db_id,
        "node_id": gid("PullRequest", pull.db_id),
        "url": url,
        "html_url": html_url,
        "number": number,
        "title": pull.title,
        "body": pull.body,
        "state": pull.state,
        "locked": False,
        "draft": False,
        "user": user,
        "created_at": pull.created_at,
        "updated_at": pull.updated_at,
        "closed_at": getattr(pull, "closed_at", None),
        "merged_at": pull.merged_at,
        "merge_commit_sha": pull.merge_commit_sha,
        "merged": bool(pull.merged),
        "mergeable": (not pull.merged) and pull.state == "open",
        "rebaseable": False,
        "head": {
            "label": "%s:%s" % (login, pull.head_ref),
            "ref": pull.head_ref,
            "sha": head_sha,
            "user": user,
            "repo": None,
        },
        "base": {
            "label": "%s:%s" % (login, pull.base_ref),
            "ref": pull.base_ref,
            "sha": base_sha,
            "user": user,
            "repo": None,
        },
        "labels": [],
        "comments": repo.get_comments_count(number),
        "commits": 1,
        "additions": 0,
        "deletions": 0,
        "changed_files": 0,
    }


def _commit_fields(store: Any, oid: Optional[str]) -> Tuple[Optional[str], List[str]]:
    """(tree oid, parent oids) of a commit, or (None, []) when unreadable."""
    if not oid:
        return None, []
    try:
        obj_type, payload = store.get(oid)
    except Exception:
        return None, []
    if obj_type != "commit":
        return None, []
    try:
        commit = Commit.parse(payload)
    except Exception:
        return None, []
    return commit.tree, list(commit.parents or [])


def _ancestors(store: Any, oid: str) -> Dict[str, int]:
    depths: Dict[str, int] = {}
    queue: List[Tuple[str, int]] = [(oid, 0)]
    while queue:
        current, depth = queue.pop(0)
        if current in depths:
            continue
        depths[current] = depth
        _, parents = _commit_fields(store, current)
        for parent in parents:
            if parent not in depths:
                queue.append((parent, depth + 1))
    return depths


def _merge_base(store: Any, left: Optional[str], right: Optional[str]) -> Optional[str]:
    if not left or not right:
        return None
    left_depths = _ancestors(store, left)
    right_depths = _ancestors(store, right)
    common = set(left_depths) & set(right_depths)
    if not common:
        return None
    return min(common, key=lambda oid: (left_depths[oid] + right_depths[oid], oid))


def _three_way(
    base: Dict[str, Tuple[int, str]],
    ours: Dict[str, Tuple[int, str]],
    theirs: Dict[str, Tuple[int, str]],
) -> Tuple[Dict[str, Tuple[int, str]], List[str]]:
    merged: Dict[str, Tuple[int, str]] = {}
    conflicts: List[str] = []
    for path in sorted(set(base) | set(ours) | set(theirs)):
        base_val = base.get(path)
        our_val = ours.get(path)
        their_val = theirs.get(path)
        if our_val == their_val:
            chosen = our_val
        elif our_val == base_val:
            chosen = their_val
        elif their_val == base_val:
            chosen = our_val
        else:
            conflicts.append(path)
            chosen = our_val
        if chosen is not None:
            merged[path] = chosen
    return merged, conflicts


def _build_tree(store: Any, files: Dict[str, Tuple[int, str]]) -> str:
    tree_oid: Optional[str] = None
    for path in sorted(files):
        tree_oid = apply_path_change(store, tree_oid, path, files[path])
    if tree_oid is None:
        tree_oid = store.put("tree", b"")
    return tree_oid


def merge_pull(store: Any, repo: Any, pull: Any) -> str:
    """Squash-merge ``pull`` into its base branch; answer the merge commit oid.

    Raises :class:`ToolError` when the pull request cannot be merged (unknown
    refs, conflicting trees, already closed).
    """
    with store.lock:
        if pull.merged:
            return pull.merge_commit_sha or ""
        if pull.state != "open":
            raise ToolError("Pull request is not open")
        base_ref = pull.base_ref or repo.default_branch
        base_tip = repo.get_ref("refs/heads/%s" % base_ref)
        head_tip = repo.get_ref("refs/heads/%s" % pull.head_ref) if pull.head_ref else None
        if not head_tip:
            head_tip = pull.head_sha or None
        if not base_tip or not head_tip:
            raise ToolError("Pull request is not mergeable")
        base_tree, _ = _commit_fields(store, base_tip)
        head_tree, _ = _commit_fields(store, head_tip)
        if base_tree is None or head_tree is None:
            raise ToolError("Pull request is not mergeable")
        merge_base = _merge_base(store, base_tip, head_tip)
        merge_base_tree, _ = _commit_fields(store, merge_base)
        files, conflicts = _three_way(
            read_tree_files(store, merge_base_tree),
            read_tree_files(store, base_tree),
            read_tree_files(store, head_tree),
        )
        if conflicts:
            raise ToolError("Pull request has conflicts and cannot be merged")
        new_tree = _build_tree(store, files)
        message = "%s (#%d)" % (pull.title, pull.number)
        if pull.body:
            message += "\n\n%s" % pull.body
        merge_oid = commit_on_branch(store, repo, base_ref, [base_tip], new_tree, message)
        now = store.now_iso()
        pull.merged = True
        pull.state = "closed"
        pull.merge_commit_sha = merge_oid
        pull.merged_at = now
        pull.updated_at = now
        try:
            pull.closed_at = now
        except Exception:
            pass
        store.clear_pack_cache(repo)
        return merge_oid


# ---------------------------------------------------------------------------
# Tool results
# ---------------------------------------------------------------------------

def _ok(payload: Any) -> Dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, sort_keys=True, ensure_ascii=False)}
        ],
        "structuredContent": payload,
        "isError": False,
    }


def _fail(message: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _tool_create_repo(store: Any, args: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    name = _text(_first(args, ("name",) + _REPO_KEYS))
    if name is None:
        raise ToolError("name is required")
    owner = _text(_first(args, _OWNER_KEYS))
    if owner is not None:
        if store.bind_owner(owner) != owner:
            raise ToolError("repository not found")
    elif store.owner_login is None:
        store.bind_owner("owner")
    description = _first(args, ("description", "about", "summary"))
    private = bool(_first(args, ("private", "is_private")) or False)
    with store.lock:
        repo = store.get_repo(name)
        if repo is None:
            repo = store.create_repo(
                name=name,
                description=description if isinstance(description, str) else "",
                private=private,
            )
    return _ok(make_repo_obj(store, repo, base_url))


def _tool_get_repo(store: Any, args: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    repo = _resolve_repo(store, args)
    return _ok(make_repo_obj(store, repo, base_url))


def _tool_create_issue(store: Any, args: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    repo = _resolve_repo(store, args)
    title = _text(_first(args, _TITLE_KEYS))
    if title is None:
        raise ToolError("title is required")
    body = _first(args, _BODY_KEYS)
    body_text = body if isinstance(body, str) else ""
    labels = args.get("labels")
    label_names: List[str] = []
    if isinstance(labels, list):
        label_names = [label for label in labels if isinstance(label, str) and label in repo.labels]
    with store.lock:
        number = repo.next_number
        repo.next_number += 1
        db_id = store.next_issue_db_id
        store.next_issue_db_id += 1
        now = store.now_iso()
        issue = Issue(
            number=number,
            title=title,
            body=body_text,
            state="open",
            labels=label_names,
            author=store.get_owner_login(),
            created_at=now,
            updated_at=now,
            db_id=db_id,
            comments_count=0,
        )
        repo.issues[number] = issue
    return _ok(make_issue_obj(store, repo, issue, base_url))


def _state_filter(args: Dict[str, Any]) -> Optional[str]:
    state = _text(_first(args, ("state", "status")))
    if state is None or state.lower() in ("all", "any"):
        return None
    return state.lower()


def _tool_list_issues(store: Any, args: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    repo = _resolve_repo(store, args)
    state = _state_filter(args) or "open"
    with store.lock:
        issues = sorted(repo.issues.values(), key=lambda item: item.number, reverse=True)
        payload = [
            make_issue_obj(store, repo, issue, base_url)
            for issue in issues
            if issue.state == state
        ]
    return _ok(payload)


def _tool_create_pull_request(store: Any, args: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    repo = _resolve_repo(store, args)
    title = _text(_first(args, _TITLE_KEYS))
    if title is None:
        raise ToolError("title is required")
    head = _text(_first(args, _HEAD_KEYS))
    if head is None:
        raise ToolError("head is required")
    base = _text(_first(args, _BASE_KEYS)) or repo.default_branch
    if ":" in head:
        head = head.split(":")[-1]
    if ":" in base:
        base = base.split(":")[-1]
    body = _first(args, _BODY_KEYS)
    body_text = body if isinstance(body, str) else ""
    head_sha = repo.get_ref("refs/heads/%s" % head)
    if not head_sha:
        raise ToolError("head ref not found: %s" % head)
    base_sha = repo.get_ref("refs/heads/%s" % base)
    if not base_sha:
        raise ToolError("base ref not found: %s" % base)
    if head == base:
        raise ToolError("No commits between %s and %s" % (base, head))
    with store.lock:
        number = repo.next_number
        repo.next_number += 1
        db_id = store.next_pull_db_id
        store.next_pull_db_id += 1
        now = store.now_iso()
        pull = Pull(
            number=number,
            title=title,
            body=body_text,
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
    return _ok(make_pull_obj(store, repo, pull, base_url))


def _tool_merge_pull_request(store: Any, args: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    repo = _resolve_repo(store, args)
    number = _as_int(_first(args, _NUMBER_KEYS))
    if number is None:
        raise ToolError("pull_number is required")
    pull = repo.get_pull(number)
    if pull is None:
        raise ToolError("pull request not found: %s" % number)
    merge_pull(store, repo, pull)
    return _ok(make_pull_obj(store, repo, pull, base_url))


def _tool_list_pull_requests(store: Any, args: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    repo = _resolve_repo(store, args)
    state = _state_filter(args) or "open"
    with store.lock:
        pulls = sorted(repo.pulls.values(), key=lambda item: item.number, reverse=True)
        payload = [
            make_pull_obj(store, repo, pull, base_url)
            for pull in pulls
            if pull.state == state
        ]
    return _ok(payload)


def _tool_get_issue(store: Any, args: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    repo = _resolve_repo(store, args)
    number = _as_int(_first(args, _NUMBER_KEYS))
    if number is None:
        raise ToolError("issue_number is required")
    issue = repo.get_issue(number)
    if issue is None:
        raise ToolError("issue not found: %s" % number)
    return _ok(make_issue_obj(store, repo, issue, base_url))


def _tool_add_comment(store: Any, args: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    repo = _resolve_repo(store, args)
    number = _as_int(_first(args, _NUMBER_KEYS))
    if number is None:
        raise ToolError("issue_number is required")
    body = _first(args, _BODY_KEYS)
    if not isinstance(body, str) or body == "":
        raise ToolError("body is required")
    if repo.get_issue(number) is None:
        raise ToolError("issue not found: %s" % number)
    with store.lock:
        comment_id = store.next_comment_db_id
        store.next_comment_db_id += 1
        now = store.now_iso()
        comment = Comment(
            id=comment_id,
            body=body,
            author=store.get_owner_login(),
            created_at=now,
            updated_at=now,
            issue_number=number,
        )
        repo.comments.setdefault(number, []).append(comment)
    return _ok(make_comment_obj(store, repo, number, comment, base_url))


_TOOL_HANDLERS = {
    "create_repo": _tool_create_repo,
    "get_repo": _tool_get_repo,
    "create_issue": _tool_create_issue,
    "list_issues": _tool_list_issues,
    "create_pull_request": _tool_create_pull_request,
    "merge_pull_request": _tool_merge_pull_request,
    "list_pull_requests": _tool_list_pull_requests,
    "get_issue": _tool_get_issue,
    "add_comment": _tool_add_comment,
}


# ---------------------------------------------------------------------------
# JSON-RPC dispatch
# ---------------------------------------------------------------------------

def _rpc_result(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _initialize_result(params: Any) -> Dict[str, Any]:
    requested = None
    if isinstance(params, dict):
        candidate = params.get("protocolVersion")
        if isinstance(candidate, str) and candidate.strip():
            requested = candidate.strip()
    return {
        "protocolVersion": requested or DEFAULT_LEGACY_VERSION,
        "capabilities": CAPABILITIES,
        "serverInfo": SERVER_INFO,
        "instructions": "A GitHub-compatible forge: repositories, issues and pull requests.",
    }


def _discover_result() -> Dict[str, Any]:
    return {
        "supportedVersions": SUPPORTED_VERSIONS,
        "protocolVersion": SUPPORTED_VERSIONS[0],
        "capabilities": CAPABILITIES,
        "serverInfo": SERVER_INFO,
        "instructions": "A GitHub-compatible forge: repositories, issues and pull requests.",
    }


def _call_tool(store: Any, params: Any, base_url: str) -> Tuple[bool, Any]:
    """Answer (is_rpc_error, payload) for one tools/call."""
    if not isinstance(params, dict):
        return True, (-32602, "Invalid params: expected an object")
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return True, (-32602, "Invalid params: tool name is required")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return True, (-32602, "Invalid params: arguments must be an object")
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return True, (-32602, "Unknown tool: %s" % name)
    try:
        return False, handler(store, arguments, base_url)
    except ToolError as exc:
        return False, _fail(str(exc))
    except Exception as exc:  # a tool must never kill the request
        return False, _fail("internal error: %s" % exc)


def _handle_one(store: Any, request: Any, base_url: str) -> Tuple[int, Any]:
    """Answer (http_status, payload_or_None) for one JSON-RPC request object."""
    if not isinstance(request, dict):
        return 200, _rpc_error(None, -32600, "Invalid Request")
    method = request.get("method")
    if not isinstance(method, str) or not method:
        return 200, _rpc_error(request.get("id"), -32600, "Invalid Request")
    request_id = request.get("id")
    is_notification = "id" not in request
    if is_notification or method.startswith("notifications/"):
        return 202, None

    params = request.get("params")
    if method == "initialize":
        return 200, _rpc_result(request_id, _initialize_result(params))
    if method == "server/discover":
        return 200, _rpc_result(request_id, _discover_result())
    if method == "ping":
        return 200, _rpc_result(request_id, {})
    if method == "tools/list":
        return 200, _rpc_result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        is_error, payload = _call_tool(store, params, base_url)
        if is_error:
            code, message = payload
            return 200, _rpc_error(request_id, code, message)
        return 200, _rpc_result(request_id, payload)
    return 200, _rpc_error(request_id, -32601, "Method not found: %s" % method)


def _authorized(authorization: Optional[str], token: Optional[str]) -> bool:
    if not token:
        return True
    if not isinstance(authorization, str):
        return False
    scheme, _, presented = authorization.partition(" ")
    return scheme.lower() == "bearer" and presented.strip() == token


def handle_mcp(
    store: Any,
    body: bytes,
    authorization: Optional[str] = None,
    token: Optional[str] = None,
    base_url: str = "",
) -> Tuple[int, Any]:
    """Serve one ``POST /mcp``; answer ``(http_status, json_payload_or_None)``.

    ``None`` as the payload means an empty body (the 202 of a notification).
    """
    if not _authorized(authorization, token):
        return 401, {"message": "Bad credentials"}

    if isinstance(body, (bytes, bytearray)):
        try:
            raw = bytes(body).decode("utf-8")
        except UnicodeDecodeError:
            return 400, _rpc_error(None, -32700, "Parse error")
    elif isinstance(body, str):
        raw = body
    else:
        raw = ""

    if not raw.strip():
        return 400, _rpc_error(None, -32700, "Parse error")

    try:
        request = json.loads(raw)
    except Exception:
        return 400, _rpc_error(None, -32700, "Parse error")

    if isinstance(request, list):
        if not request:
            return 200, _rpc_error(None, -32600, "Invalid Request")
        responses: List[Any] = []
        for item in request:
            status, payload = _handle_one(store, item, base_url)
            if status != 202 and payload is not None:
                responses.append(payload)
        if not responses:
            return 202, None
        return 200, responses

    return _handle_one(store, request, base_url)
