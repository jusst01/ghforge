"""Mini GraphQL engine (tokenizer, parser, executor) and the GitHub-shaped schema.

The engine supports the subset of the GraphQL language the forge's clients send:
named query/mutation operations with variable definitions, field aliases,
arguments with every literal form, inline fragments, fragment spreads, nested
selection sets and the introspection fields ``__type`` / ``__typename``.

Execution is driven by a flat schema table: ``SCHEMA[Type][field] = (return_type,
resolver)``.  A field missing from the table is reported as a GraphQL error and
resolved to null without failing its siblings.
"""

import base64
import json
from typing import Any, Dict, List, Optional, Tuple

try:
    from forge.store import gid, parse_gid
    from forge.rest import read_tree_files, apply_path_change, commit_on_branch
except ImportError:  # pragma: no cover - direct module import
    from store import gid, parse_gid
    from rest import read_tree_files, apply_path_change, commit_on_branch


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class GraphQLSyntaxError(Exception):
    """Raised by the tokenizer/parser on a malformed document."""


class GraphQLExecutionError(Exception):
    """Raised by a resolver to report a GraphQL-level error for one field."""


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

_PUNCTUATORS = set("!$():=@[]{|}&")
_NAME_START = set("_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_NAME_CONT = _NAME_START | set("0123456789")
_DIGITS = set("0123456789")


def _tokenize(source: str) -> List[Tuple[str, Any]]:
    """Split a document into (kind, value) tokens; commas and comments vanish."""
    tokens: List[Tuple[str, Any]] = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch in " \t\r\n,﻿":
            i += 1
            continue
        if ch == "#":
            while i < n and source[i] not in "\r\n":
                i += 1
            continue
        if ch == ".":
            if source.startswith("...", i):
                tokens.append(("PUNCT", "..."))
                i += 3
                continue
            raise GraphQLSyntaxError("Unexpected character '.'")
        if ch in _PUNCTUATORS:
            tokens.append(("PUNCT", ch))
            i += 1
            continue
        if ch == '"':
            if source.startswith('"""', i):
                value, i = _read_block_string(source, i + 3)
                tokens.append(("STRING", value))
            else:
                value, i = _read_string(source, i + 1)
                tokens.append(("STRING", value))
            continue
        if ch == "-" or ch in _DIGITS:
            value, is_float, i = _read_number(source, i)
            tokens.append(("FLOAT" if is_float else "INT", value))
            continue
        if ch in _NAME_START:
            start = i
            while i < n and source[i] in _NAME_CONT:
                i += 1
            tokens.append(("NAME", source[start:i]))
            continue
        raise GraphQLSyntaxError("Unexpected character %r" % ch)
    tokens.append(("EOF", None))
    return tokens


def _read_number(source: str, i: int) -> Tuple[Any, bool, int]:
    start = i
    is_float = False
    if source[i] == "-":
        i += 1
    while i < len(source) and source[i] in _DIGITS:
        i += 1
    if i < len(source) and source[i] == ".":
        is_float = True
        i += 1
        while i < len(source) and source[i] in _DIGITS:
            i += 1
    if i < len(source) and source[i] in "eE":
        is_float = True
        i += 1
        if i < len(source) and source[i] in "+-":
            i += 1
        while i < len(source) and source[i] in _DIGITS:
            i += 1
    text = source[start:i]
    try:
        return (float(text) if is_float else int(text)), is_float, i
    except ValueError:
        raise GraphQLSyntaxError("Invalid number %r" % text)


_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
            "n": "\n", "r": "\r", "t": "\t"}


def _read_string(source: str, i: int) -> Tuple[str, int]:
    out: List[str] = []
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == '"':
            return "".join(out), i + 1
        if ch == "\\":
            i += 1
            if i >= n:
                break
            esc = source[i]
            if esc == "u":
                hex_digits = source[i + 1:i + 5]
                try:
                    out.append(chr(int(hex_digits, 16)))
                except ValueError:
                    raise GraphQLSyntaxError("Invalid unicode escape")
                i += 5
                continue
            out.append(_ESCAPES.get(esc, esc))
            i += 1
            continue
        out.append(ch)
        i += 1
    raise GraphQLSyntaxError("Unterminated string")


def _read_block_string(source: str, i: int) -> Tuple[str, int]:
    end = source.find('"""', i)
    if end < 0:
        raise GraphQLSyntaxError("Unterminated block string")
    raw = source[i:end]
    lines = raw.split("\n")
    if len(lines) > 1:
        indent = min((len(line) - len(line.lstrip(" \t")) for line in lines[1:] if line.strip()), default=0)
        lines = [lines[0]] + [line[indent:] for line in lines[1:]]
    return "\n".join(lines), end + 3


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class _Parser:
    def __init__(self, source: str):
        self.tokens = _tokenize(source)
        self.pos = 0

    # -- token helpers ----------------------------------------------------
    def peek(self) -> Tuple[str, Any]:
        return self.tokens[self.pos]

    def advance(self) -> Tuple[str, Any]:
        tok = self.tokens[self.pos]
        if tok[0] != "EOF":
            self.pos += 1
        return tok

    def at_punct(self, value: str) -> bool:
        tok = self.peek()
        return tok[0] == "PUNCT" and tok[1] == value

    def at_name(self, value: Optional[str] = None) -> bool:
        tok = self.peek()
        if tok[0] != "NAME":
            return False
        return value is None or tok[1] == value

    def expect_punct(self, value: str) -> None:
        if not self.at_punct(value):
            raise GraphQLSyntaxError("Expected %r" % value)
        self.advance()

    def expect_name(self) -> str:
        if not self.at_name():
            raise GraphQLSyntaxError("Expected a name")
        return self.advance()[1]

    # -- document ---------------------------------------------------------
    def parse_document(self) -> Dict[str, Any]:
        operations: List[Dict[str, Any]] = []
        fragments: Dict[str, Dict[str, Any]] = {}
        while self.peek()[0] != "EOF":
            if self.at_name("fragment"):
                self.advance()
                name = self.expect_name()
                if not self.at_name("on"):
                    raise GraphQLSyntaxError("Expected 'on' in a fragment definition")
                self.advance()
                type_cond = self.expect_name()
                self._skip_directives()
                fragments[name] = {"name": name, "type": type_cond,
                                   "selections": self.parse_selection_set()}
            else:
                operations.append(self.parse_operation())
        return {"operations": operations, "fragments": fragments}

    def parse_operation(self) -> Dict[str, Any]:
        op_type = "query"
        name = None
        if self.at_name("query") or self.at_name("mutation") or self.at_name("subscription"):
            op_type = self.advance()[1]
            if self.at_name():
                name = self.advance()[1]
        var_defs = self.parse_variable_definitions()
        self._skip_directives()
        return {"op": op_type, "name": name, "variables": var_defs,
                "selections": self.parse_selection_set()}

    def parse_variable_definitions(self) -> List[Dict[str, Any]]:
        defs: List[Dict[str, Any]] = []
        if not self.at_punct("("):
            return defs
        self.advance()
        while not self.at_punct(")"):
            self.expect_punct("$")
            name = self.expect_name()
            self.expect_punct(":")
            self._parse_type_ref()
            default = None
            if self.at_punct("="):
                self.advance()
                default = self.parse_value()
            self._skip_directives()
            defs.append({"name": name, "default": default})
        self.advance()
        return defs

    def _parse_type_ref(self) -> None:
        if self.at_punct("["):
            self.advance()
            self._parse_type_ref()
            self.expect_punct("]")
        else:
            self.expect_name()
        if self.at_punct("!"):
            self.advance()

    def parse_selection_set(self) -> List[Dict[str, Any]]:
        self.expect_punct("{")
        selections: List[Dict[str, Any]] = []
        while not self.at_punct("}"):
            if self.peek()[0] == "EOF":
                raise GraphQLSyntaxError("Unterminated selection set")
            selections.append(self.parse_selection())
        self.advance()
        return selections

    def parse_selection(self) -> Dict[str, Any]:
        if self.at_punct("..."):
            self.advance()
            if self.at_name("on"):
                self.advance()
                type_cond = self.expect_name()
                self._skip_directives()
                return {"kind": "inline", "type": type_cond,
                        "selections": self.parse_selection_set()}
            if self.at_name():
                name = self.advance()[1]
                self._skip_directives()
                return {"kind": "spread", "name": name}
            self._skip_directives()
            return {"kind": "inline", "type": None,
                    "selections": self.parse_selection_set()}
        name = self.expect_name()
        alias = None
        if self.at_punct(":"):
            self.advance()
            alias = name
            name = self.expect_name()
        args = self.parse_arguments()
        self._skip_directives()
        selections = None
        if self.at_punct("{"):
            selections = self.parse_selection_set()
        return {"kind": "field", "alias": alias, "name": name,
                "args": args, "selections": selections}

    def parse_arguments(self) -> Dict[str, Any]:
        args: Dict[str, Any] = {}
        if not self.at_punct("("):
            return args
        self.advance()
        while not self.at_punct(")"):
            if self.peek()[0] == "EOF":
                raise GraphQLSyntaxError("Unterminated argument list")
            key = self.expect_name()
            self.expect_punct(":")
            args[key] = self.parse_value()
        self.advance()
        return args

    def parse_value(self) -> Dict[str, Any]:
        tok = self.peek()
        kind, value = tok
        if kind == "PUNCT":
            if value == "$":
                self.advance()
                return {"kind": "var", "name": self.expect_name()}
            if value == "[":
                self.advance()
                items = []
                while not self.at_punct("]"):
                    if self.peek()[0] == "EOF":
                        raise GraphQLSyntaxError("Unterminated list value")
                    items.append(self.parse_value())
                self.advance()
                return {"kind": "list", "values": items}
            if value == "{":
                self.advance()
                fields: Dict[str, Any] = {}
                while not self.at_punct("}"):
                    if self.peek()[0] == "EOF":
                        raise GraphQLSyntaxError("Unterminated object value")
                    key = self.expect_name()
                    self.expect_punct(":")
                    fields[key] = self.parse_value()
                self.advance()
                return {"kind": "object", "fields": fields}
            raise GraphQLSyntaxError("Unexpected punctuator %r in value" % value)
        if kind == "INT":
            self.advance()
            return {"kind": "int", "value": value}
        if kind == "FLOAT":
            self.advance()
            return {"kind": "float", "value": value}
        if kind == "STRING":
            self.advance()
            return {"kind": "str", "value": value}
        if kind == "NAME":
            self.advance()
            if value == "true":
                return {"kind": "bool", "value": True}
            if value == "false":
                return {"kind": "bool", "value": False}
            if value == "null":
                return {"kind": "null"}
            return {"kind": "enum", "value": value}
        raise GraphQLSyntaxError("Unexpected end of document in value")

    def _skip_directives(self) -> None:
        while self.at_punct("@"):
            self.advance()
            self.expect_name()
            if self.at_punct("("):
                self.parse_arguments()


def parse(source: str) -> Dict[str, Any]:
    """Parse a GraphQL document into operations and fragment definitions."""
    return _Parser(source).parse_document()


# ---------------------------------------------------------------------------
# Argument / variable handling
# ---------------------------------------------------------------------------

def _resolve_value(node: Optional[Dict[str, Any]], variables: Dict[str, Any]) -> Any:
    if node is None:
        return None
    kind = node.get("kind")
    if kind == "var":
        return variables.get(node.get("name"))
    if kind in ("int", "float", "str", "bool"):
        return node.get("value")
    if kind == "null":
        return None
    if kind == "enum":
        return node.get("value")
    if kind == "list":
        return [_resolve_value(item, variables) for item in node.get("values", [])]
    if kind == "object":
        return {key: _resolve_value(val, variables) for key, val in node.get("fields", {}).items()}
    return None


def _resolve_args(args: Dict[str, Any], variables: Dict[str, Any]) -> Dict[str, Any]:
    return {key: _resolve_value(val, variables) for key, val in args.items()}


# ---------------------------------------------------------------------------
# Cursor helpers and object wrappers
# ---------------------------------------------------------------------------

def _cursor_encode(offset: int) -> str:
    return base64.b64encode(str(offset).encode("ascii")).decode("ascii")


def _cursor_decode(cursor: Any) -> int:
    if not isinstance(cursor, str) or not cursor:
        return 0
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4))
        return max(0, int(raw.decode("ascii")))
    except Exception:
        try:
            return max(0, int(cursor))
        except Exception:
            return 0


def _connection(nodes: List[Dict[str, Any]], args: Dict[str, Any], type_name: str) -> Dict[str, Any]:
    total = len(nodes)
    start = min(_cursor_decode(args.get("after")), total)
    first = args.get("first")
    if not isinstance(first, int) or first < 0:
        first = None
    end = total if first is None else min(start + first, total)
    page = nodes[start:end]
    return {
        "__typename": type_name,
        "nodes": page,
        "totalCount": total,
        "pageInfo": {
            "__typename": "PageInfo",
            "hasNextPage": end < total,
            "hasPreviousPage": start > 0,
            "startCursor": _cursor_encode(start) if page else None,
            "endCursor": _cursor_encode(end) if page else None,
        },
    }


def _repo_obj(repo: Any) -> Dict[str, Any]:
    return {"__typename": "Repository", "repo": repo}


def _issue_obj(repo: Any, issue: Any) -> Dict[str, Any]:
    return {"__typename": "Issue", "repo": repo, "issue": issue}


def _pull_obj(repo: Any, pull: Any) -> Dict[str, Any]:
    return {"__typename": "PullRequest", "repo": repo, "pull": pull}


def _user_obj(store: Any, login: str, is_viewer: bool = False) -> Optional[Dict[str, Any]]:
    user = None
    lookup = getattr(store, "user_by_login", None)
    if callable(lookup):
        user = lookup(login)
    else:
        for candidate in getattr(store, "users", None) or []:
            if isinstance(candidate, dict) and candidate.get("login") == login:
                user = candidate
                break
    if user is None and is_viewer:
        # An unknown login still answers a user for the viewer resolver: use the
        # store's viewer record so id/name/email stay seed-derived.
        viewer = getattr(store, "viewer", None)
        if isinstance(viewer, dict):
            user = viewer
    if user:
        return {"__typename": "User",
                "db_id": int(user["id"]),
                "login": user.get("login"),
                "name": user.get("name"),
                "email": user.get("email")}
    if is_viewer:
        return {"__typename": "User", "login": login, "db_id": 1000}
    return None


def _enum_state(value: str) -> str:
    return (value or "open").upper()


# ---------------------------------------------------------------------------
# Git helpers: commit reading, merge base, three-way merge, changed files
# ---------------------------------------------------------------------------

def _commit_tree_and_parents(store: Any, oid: Optional[str]) -> Tuple[Optional[str], List[str]]:
    if not oid:
        return None, []
    try:
        obj_type, payload = store.get(oid)
    except Exception:
        return None, []
    if obj_type != "commit":
        return None, []
    head = payload.split(b"\n\n", 1)[0]
    tree = None
    parents: List[str] = []
    for line in head.split(b"\n"):
        if line.startswith(b"tree "):
            tree = line[5:].decode("ascii", "replace").strip()
        elif line.startswith(b"parent "):
            parents.append(line[7:].decode("ascii", "replace").strip())
    return tree, parents


def _tree_of(store: Any, oid: Optional[str]) -> Optional[str]:
    tree, _ = _commit_tree_and_parents(store, oid)
    return tree


def _ancestors(store: Any, oid: str) -> List[str]:
    seen: List[str] = []
    visited = set()
    queue = [oid]
    while queue:
        current = queue.pop(0)
        if not current or current in visited:
            continue
        visited.add(current)
        seen.append(current)
        _, parents = _commit_tree_and_parents(store, current)
        queue.extend(parents)
    return seen


def _merge_base(store: Any, a: Optional[str], b: Optional[str]) -> Optional[str]:
    if not a or not b:
        return None
    a_set = set(_ancestors(store, a))
    visited = set()
    queue = [b]
    while queue:
        current = queue.pop(0)
        if not current or current in visited:
            continue
        visited.add(current)
        if current in a_set:
            return current
        _, parents = _commit_tree_and_parents(store, current)
        queue.extend(parents)
    return None


def _three_way_merge(store: Any, base_oid: Optional[str], ours_oid: Optional[str],
                     theirs_oid: Optional[str]) -> Tuple[Dict[str, Tuple[int, str]], List[str]]:
    """Merge three trees by path; answer (merged_files, conflicting_paths)."""
    base = read_tree_files(store, base_oid)
    ours = read_tree_files(store, ours_oid)
    theirs = read_tree_files(store, theirs_oid)
    merged: Dict[str, Tuple[int, str]] = {}
    conflicts: List[str] = []
    for path in sorted(set(base) | set(ours) | set(theirs)):
        b_val = base.get(path)
        o_val = ours.get(path)
        t_val = theirs.get(path)
        if o_val == t_val:
            chosen = o_val
        elif o_val == b_val:
            chosen = t_val
        elif t_val == b_val:
            chosen = o_val
        else:
            conflicts.append(path)
            chosen = o_val
        if chosen is not None:
            merged[path] = chosen
    return merged, conflicts


def _build_tree(store: Any, files: Dict[str, Tuple[int, str]]) -> Optional[str]:
    tree_oid: Optional[str] = None
    for path, mode_and_oid in files.items():
        tree_oid = apply_path_change(store, tree_oid, path, mode_and_oid)
    if tree_oid is None:
        tree_oid = store.put("tree", b"")
    return tree_oid


def _changed_paths(store: Any, base_oid: Optional[str], head_oid: Optional[str]) -> List[str]:
    base = read_tree_files(store, base_oid)
    head = read_tree_files(store, head_oid)
    return sorted(path for path in set(base) | set(head) if base.get(path) != head.get(path))


# ---------------------------------------------------------------------------
# Resolvers — Query
# ---------------------------------------------------------------------------

def _find_repo_by_db_id(store: Any, db_id: int) -> Any:
    for repo in store.repos.values():
        if repo.db_id == db_id:
            return repo
    return None


def _resolve_viewer(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return _user_obj(ctx["store"], ctx["login"], is_viewer=True)


def _resolve_repository(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    owner = args.get("owner")
    name = args.get("name")
    if owner is not None and owner != ctx["login"]:
        return None
    if not isinstance(name, str):
        return None
    repo = ctx["store"].get_repo(name)
    if repo is None:
        return None
    return _repo_obj(repo)


def _resolve_node(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    kind, db_id = parse_gid(args.get("id"))
    store = ctx["store"]
    if kind == "Repository":
        repo = _find_repo_by_db_id(store, db_id)
        return _repo_obj(repo) if repo is not None else None
    if kind == "User":
        return _user_obj(ctx["store"], ctx["login"], is_viewer=True)
    if kind in ("Issue", "PullRequest"):
        for repo in store.repos.values():
            records = repo.issues.values() if kind == "Issue" else repo.pulls.values()
            for record in records:
                if record.db_id == db_id:
                    if kind == "Issue":
                        return _issue_obj(repo, record)
                    return _pull_obj(repo, record)
    if kind == "IssueComment":
        for repo in store.repos.values():
            for comments in repo.comments.values():
                for comment in comments:
                    if comment.id == db_id:
                        return {"__typename": "IssueComment", "repo": repo,
                                "issue_number": comment.issue_number, "comment": comment}
    return None


def _resolve_type(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = args.get("name")
    if not isinstance(name, str):
        return None
    if name not in SCHEMA and name not in ENUM_VALUES:
        return None
    return {"__typename": "__Type", "name": name}


# ---------------------------------------------------------------------------
# Resolvers — User / Ref / Commit / Label / Comment / PageInfo
# ---------------------------------------------------------------------------

def _user_login(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return obj.get("login") or ctx["login"]


def _user_id(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return gid("User", obj.get("db_id", 1000))


def _user_database_id(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> int:
    return int(obj.get("db_id", 1000))


def _ref_name(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return obj.get("name") or ""


def _ref_target(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return obj.get("target")


def _commit_oid(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return obj.get("oid") or ""


def _label_name(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return obj.get("name") or ""


def _label_color(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return obj.get("color") or "ededed"


def _comment_body(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    comment = obj.get("comment")
    return getattr(comment, "body", "") if comment is not None else obj.get("body", "")


def _comment_id(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    comment = obj.get("comment")
    db_id = getattr(comment, "id", obj.get("db_id", 0)) if comment is not None else obj.get("db_id", 0)
    return gid("IssueComment", int(db_id))


# ---------------------------------------------------------------------------
# Resolvers — Repository
# ---------------------------------------------------------------------------

def _repo_id(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return gid("Repository", obj["repo"].db_id)


def _repo_database_id(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> int:
    return int(obj["repo"].db_id)


def _repo_name(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return obj["repo"].name


def _repo_name_with_owner(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return "%s/%s" % (ctx["login"], obj["repo"].name)


def _repo_owner(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return _user_obj(ctx["store"], ctx["login"])


def _repo_default_branch_ref(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    repo = obj["repo"]
    tip = repo.get_ref("refs/heads/%s" % repo.default_branch)
    target = {"__typename": "Commit", "oid": tip} if tip else None
    return {"__typename": "Ref", "name": repo.default_branch, "target": target}


def _repo_viewer_permission(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return "ADMIN"


def _repo_has_issues(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> bool:
    return True


def _repo_description(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return obj["repo"].description or ""


def _repo_private(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> bool:
    return bool(obj["repo"].private)


def _repo_url(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return "%s/%s/%s" % (ctx.get("base_url", ""), ctx["login"], obj["repo"].name)


def _repo_created_at(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[str]:
    return getattr(obj["repo"], "created_at", None) or None


def _repo_updated_at(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[str]:
    return getattr(obj["repo"], "updated_at", None) or None


def _repo_issue(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    number = args.get("number")
    if not isinstance(number, int):
        return None
    issue = obj["repo"].issues.get(number)
    return _issue_obj(obj["repo"], issue) if issue is not None else None


def _filter_states(records: List[Any], states: Any, merged_aware: bool = False) -> List[Any]:
    if not states:
        return records
    if isinstance(states, str):
        states = [states]
    wanted = set()
    for state in states:
        if isinstance(state, str):
            wanted.add(state.upper())
    out = []
    for record in records:
        if merged_aware and getattr(record, "merged", False):
            state = "MERGED"
        else:
            state = _enum_state(getattr(record, "state", "open"))
        if state in wanted:
            out.append(record)
    return out


def _order_records(records: List[Any], order_by: Any) -> List[Any]:
    if not isinstance(order_by, dict):
        return records
    field = order_by.get("field")
    direction = str(order_by.get("direction") or "ASC").upper()
    reverse = direction == "DESC"
    if field == "CREATED_AT":
        key = lambda r: (getattr(r, "created_at", "") or "", getattr(r, "number", 0))
    elif field == "UPDATED_AT":
        key = lambda r: (getattr(r, "updated_at", "") or "", getattr(r, "number", 0))
    elif field == "COMMENTS":
        key = lambda r: (getattr(r, "comments_count", 0) or 0, getattr(r, "number", 0))
    else:
        return records
    return sorted(records, key=key, reverse=reverse)


def _repo_issues(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    repo = obj["repo"]
    records = sorted(repo.issues.values(), key=lambda i: i.number)
    records = _filter_states(records, args.get("states"))
    records = _order_records(records, args.get("orderBy"))
    nodes = [_issue_obj(repo, issue) for issue in records]
    return _connection(nodes, args, "IssueConnection")


def _repo_pull_request(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    number = args.get("number")
    if not isinstance(number, int):
        return None
    pull = obj["repo"].pulls.get(number)
    return _pull_obj(obj["repo"], pull) if pull is not None else None


def _repo_pull_requests(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    repo = obj["repo"]
    records = sorted(repo.pulls.values(), key=lambda p: p.number)
    records = _filter_states(records, args.get("states"), merged_aware=True)
    nodes = [_pull_obj(repo, pull) for pull in records]
    return _connection(nodes, args, "PullRequestConnection")


# ---------------------------------------------------------------------------
# Resolvers — Issue
# ---------------------------------------------------------------------------

def _issue_id(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return gid("Issue", obj["issue"].db_id)


def _issue_database_id(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> int:
    return int(obj["issue"].db_id)


def _issue_number(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> int:
    return int(obj["issue"].number)


def _issue_title(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return obj["issue"].title


def _issue_body(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return obj["issue"].body or ""


def _issue_state(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return _enum_state(obj["issue"].state)


def _issue_labels(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    repo = obj["repo"]
    issue = obj["issue"]
    nodes = []
    for name in issue.labels or []:
        label = repo.labels.get(name)
        nodes.append({"__typename": "Label", "name": name,
                      "color": getattr(label, "color", "ededed")})
    return {"__typename": "LabelConnection", "nodes": nodes,
            "totalCount": len(nodes), "pageInfo": None}


def _issue_author(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return _user_obj(ctx["store"], ctx["login"], is_viewer=True)


def _issue_repository(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    return _repo_obj(obj["repo"])


def _issue_created_at(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[str]:
    return obj["issue"].created_at or None


def _issue_updated_at(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[str]:
    return obj["issue"].updated_at or None


def _issue_closed_at(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[str]:
    issue = obj["issue"]
    return getattr(issue, "closed_at", None)


def _issue_comments(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    repo = obj["repo"]
    issue = obj["issue"]
    comments = repo.comments.get(issue.number, [])
    nodes = [{"__typename": "IssueComment", "comment": comment,
              "repo": repo, "issue_number": issue.number} for comment in comments]
    return _connection(nodes, args, "IssueCommentConnection")


# ---------------------------------------------------------------------------
# Resolvers — PullRequest
# ---------------------------------------------------------------------------

def _pull_id(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return gid("PullRequest", obj["pull"].db_id)


def _pull_database_id(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> int:
    return int(obj["pull"].db_id)


def _pull_number(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> int:
    return int(obj["pull"].number)


def _pull_title(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return obj["pull"].title


def _pull_body(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return obj["pull"].body or ""


def _pull_state(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    pull = obj["pull"]
    if pull.merged:
        return "MERGED"
    return _enum_state(pull.state)


def _pull_merged(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> bool:
    return bool(obj["pull"].merged)


def _pull_head_ref(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return obj["pull"].head_ref


def _pull_base_ref(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return obj["pull"].base_ref


def _pull_head_oid(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[str]:
    pull = obj["pull"]
    return pull.head_sha or obj["repo"].get_ref("refs/heads/%s" % pull.head_ref)


def _pull_base_oid(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[str]:
    pull = obj["pull"]
    return pull.base_sha or obj["repo"].get_ref("refs/heads/%s" % pull.base_ref)


def _pull_mergeable(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    store = ctx["store"]
    repo = obj["repo"]
    pull = obj["pull"]
    if pull.merged:
        return "UNKNOWN"
    try:
        head_tip = pull.head_sha or repo.get_ref("refs/heads/%s" % pull.head_ref)
        base_tip = repo.get_ref("refs/heads/%s" % pull.base_ref) or pull.base_sha
        if not head_tip or not base_tip:
            return "UNKNOWN"
        merge_base = _merge_base(store, base_tip, head_tip)
        _, conflicts = _three_way_merge(store, _tree_of(store, merge_base),
                                        _tree_of(store, base_tip), _tree_of(store, head_tip))
        return "CONFLICTING" if conflicts else "MERGEABLE"
    except Exception:
        return "UNKNOWN"


def _pull_files(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    store = ctx["store"]
    repo = obj["repo"]
    pull = obj["pull"]
    head_tip = pull.head_sha or repo.get_ref("refs/heads/%s" % pull.head_ref)
    base_tip = pull.base_sha or repo.get_ref("refs/heads/%s" % pull.base_ref)
    paths = _changed_paths(store, _tree_of(store, base_tip), _tree_of(store, head_tip))
    nodes = [{"__typename": "PullRequestChangedFile", "path": path} for path in paths]
    return _connection(nodes, args, "PullRequestChangedFileConnection")


def _pull_author(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return _user_obj(ctx["store"], ctx["login"], is_viewer=True)


def _pull_repository(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    return _repo_obj(obj["repo"])


def _pull_created_at(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[str]:
    return obj["pull"].created_at or None


def _pull_updated_at(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[str]:
    return obj["pull"].updated_at or None


def _pull_closed_at(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[str]:
    return getattr(obj["pull"], "closed_at", None)


def _pull_merged_at(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[str]:
    return obj["pull"].merged_at


def _pull_merge_commit(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sha = obj["pull"].merge_commit_sha
    return {"__typename": "Commit", "oid": sha} if sha else None


def _pull_url(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return "%s/%s/%s/pull/%d" % (ctx.get("base_url", ""), ctx["login"],
                                 obj["repo"].name, obj["pull"].number)


# ---------------------------------------------------------------------------
# Resolvers — introspection
# ---------------------------------------------------------------------------

def _type_fields(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = obj.get("name")
    return [{"__typename": "__Field", "name": field} for field in FIELD_LISTS.get(name, [])]


def _type_enum_values(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = obj.get("name")
    return [{"__typename": "__EnumValue", "name": value} for value in ENUM_VALUES.get(name, [])]


def _named(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[str]:
    return obj.get("name")


def _field_name(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    return obj.get("name") or ""


# ---------------------------------------------------------------------------
# Resolvers — mutations
# ---------------------------------------------------------------------------

def _require_input(args: Dict[str, Any]) -> Dict[str, Any]:
    value = args.get("input")
    if not isinstance(value, dict):
        raise GraphQLExecutionError("Expected an input object")
    return value


def _repo_from_gid(store: Any, repository_id: Any) -> Any:
    kind, db_id = parse_gid(repository_id)
    if kind != "Repository":
        raise GraphQLExecutionError("Could not resolve to a Repository with the global id %r" % (repository_id,))
    repo = _find_repo_by_db_id(store, db_id)
    if repo is None:
        raise GraphQLExecutionError("Could not resolve to a Repository with the global id %r" % (repository_id,))
    return repo


def _find_issue_by_gid(store: Any, issue_id: Any) -> Tuple[Any, Any]:
    kind, db_id = parse_gid(issue_id)
    if kind not in ("Issue", "PullRequest"):
        raise GraphQLExecutionError("Could not resolve to an Issue with the global id %r" % (issue_id,))
    for repo in store.repos.values():
        records = repo.issues.values() if kind == "Issue" else repo.pulls.values()
        for record in records:
            if record.db_id == db_id:
                return repo, record
    raise GraphQLExecutionError("Could not resolve to an Issue with the global id %r" % (issue_id,))


def _resolve_create_issue(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    store = ctx["store"]
    data = _require_input(args)
    repo = _repo_from_gid(store, data.get("repositoryId"))
    title = data.get("title")
    if not isinstance(title, str) or not title:
        raise GraphQLExecutionError("Title can't be blank")
    with store.lock:
        number = repo.next_number
        repo.next_number += 1
        db_id = store.next_issue_db_id
        store.next_issue_db_id += 1
        now = store.now_iso()
        try:
            from forge.store import Issue
        except ImportError:  # pragma: no cover
            from store import Issue
        issue = Issue(number=number, title=title, body=data.get("body") or "",
                      state="open", labels=[], author=ctx["login"],
                      created_at=now, updated_at=now, db_id=db_id)
        repo.issues[number] = issue
    return {"__typename": "CreateIssuePayload", "issue": _issue_obj(repo, issue)}


def _resolve_close_issue(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    store = ctx["store"]
    data = _require_input(args)
    with store.lock:
        repo, issue = _find_issue_by_gid(store, data.get("issueId"))
        issue.state = "closed"
        now = store.now_iso()
        issue.updated_at = now
        try:
            issue.closed_at = now
        except Exception:
            pass
    payload = _pull_obj(repo, issue) if type(issue).__name__ == "Pull" else _issue_obj(repo, issue)
    return {"__typename": "CloseIssuePayload", "issue": payload}


def _resolve_create_pull_request(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    store = ctx["store"]
    data = _require_input(args)
    repo = _repo_from_gid(store, data.get("repositoryId"))
    head_ref = data.get("headRefName")
    base_ref = data.get("baseRefName")
    title = data.get("title")
    if not isinstance(head_ref, str) or not head_ref:
        raise GraphQLExecutionError("Head ref can't be blank")
    if not isinstance(base_ref, str) or not base_ref:
        raise GraphQLExecutionError("Base ref can't be blank")
    if not isinstance(title, str) or not title:
        raise GraphQLExecutionError("Title can't be blank")
    head_sha = repo.get_ref("refs/heads/%s" % head_ref)
    base_sha = repo.get_ref("refs/heads/%s" % base_ref)
    if head_sha is None:
        raise GraphQLExecutionError("Head sha can't be blank")
    if base_sha is None:
        raise GraphQLExecutionError("Base sha can't be blank")
    if head_ref == base_ref:
        raise GraphQLExecutionError("No commits between %s and %s" % (base_ref, head_ref))
    with store.lock:
        for existing in repo.pulls.values():
            if existing.state == "open" and existing.head_ref == head_ref and existing.base_ref == base_ref:
                raise GraphQLExecutionError("A pull request already exists for %s:%s." % (ctx["login"], head_ref))
        try:
            from forge.store import Pull
        except ImportError:  # pragma: no cover
            from store import Pull
        number = repo.next_number
        repo.next_number += 1
        db_id = store.next_pull_db_id
        store.next_pull_db_id += 1
        now = store.now_iso()
        pull = Pull(number=number, title=title, body=data.get("body") or "",
                    state="open", head_ref=head_ref, base_ref=base_ref,
                    head_sha=head_sha, base_sha=base_sha, merged=False,
                    author=ctx["login"], created_at=now, updated_at=now,
                    db_id=db_id)
        repo.pulls[number] = pull
    return {"__typename": "CreatePullRequestPayload", "pullRequest": _pull_obj(repo, pull)}


def _resolve_merge_pull_request(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    store = ctx["store"]
    data = _require_input(args)
    with store.lock:
        repo, pull = _find_issue_by_gid(store, data.get("pullRequestId"))
        if type(pull).__name__ != "Pull":
            raise GraphQLExecutionError("Could not resolve to a PullRequest with the global id %r"
                                        % (data.get("pullRequestId"),))
        if pull.merged:
            return {"__typename": "MergePullRequestPayload", "pullRequest": _pull_obj(repo, pull)}
        if pull.state != "open":
            raise GraphQLExecutionError("Pull request is not open")
        base_tip = repo.get_ref("refs/heads/%s" % pull.base_ref)
        head_tip = pull.head_sha or repo.get_ref("refs/heads/%s" % pull.head_ref)
        if not base_tip or not head_tip:
            raise GraphQLExecutionError("Pull request is not mergeable")
        merge_base = _merge_base(store, base_tip, head_tip)
        merged_files, conflicts = _three_way_merge(store, _tree_of(store, merge_base),
                                                   _tree_of(store, base_tip),
                                                   _tree_of(store, head_tip))
        if conflicts:
            raise GraphQLExecutionError("Pull request is not mergeable")
        new_tree = _build_tree(store, merged_files)
        message = "%s (#%d)" % (pull.title, pull.number)
        if pull.body:
            message += "\n\n%s" % pull.body
        merge_oid = commit_on_branch(store, repo, pull.base_ref, [base_tip], new_tree, message)
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
    return {"__typename": "MergePullRequestPayload", "pullRequest": _pull_obj(repo, pull)}


def _resolve_add_comment(obj: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    store = ctx["store"]
    data = _require_input(args)
    body = data.get("body")
    if not isinstance(body, str):
        raise GraphQLExecutionError("Body can't be blank")
    with store.lock:
        repo, subject = _find_issue_by_gid(store, data.get("subjectId"))
        try:
            from forge.store import Comment
        except ImportError:  # pragma: no cover
            from store import Comment
        comment_id = store.next_comment_db_id
        store.next_comment_db_id += 1
        now = store.now_iso()
        comment = Comment(id=comment_id, body=body, author=ctx["login"],
                          created_at=now, updated_at=now,
                          issue_number=subject.number)
        repo.comments.setdefault(subject.number, []).append(comment)
    return {
        "__typename": "AddCommentPayload",
        "commentEdge": {
            "__typename": "IssueCommentEdge",
            "node": {"__typename": "IssueComment", "comment": comment,
                     "repo": repo, "issue_number": subject.number},
        },
    }


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _field(return_type: str, resolver: Any) -> Tuple[str, Any]:
    return (return_type, resolver)


SCHEMA: Dict[str, Dict[str, Tuple[str, Any]]] = {
    "Query": {
        "viewer": _field("User", _resolve_viewer),
        "repository": _field("Repository", _resolve_repository),
        "node": _field("Node", _resolve_node),
        "__type": _field("__Type", _resolve_type),
    },
    "Mutation": {
        "createIssue": _field("CreateIssuePayload", _resolve_create_issue),
        "closeIssue": _field("CloseIssuePayload", _resolve_close_issue),
        "createPullRequest": _field("CreatePullRequestPayload", _resolve_create_pull_request),
        "mergePullRequest": _field("MergePullRequestPayload", _resolve_merge_pull_request),
        "addComment": _field("AddCommentPayload", _resolve_add_comment),
    },
    "User": {
        "login": _field("String", _user_login),
        "id": _field("ID", _user_id),
        "databaseId": _field("Int", _user_database_id),
        "name": _field("String", lambda o, a, c: o.get("name") or o.get("login")),
        "email": _field("String", lambda o, a, c: o.get("email")),
        "avatarUrl": _field("URI", lambda o, a, c: ""),
        "url": _field("URI", lambda o, a, c: ""),
    },
    "Repository": {
        "id": _field("ID", _repo_id),
        "databaseId": _field("Int", _repo_database_id),
        "name": _field("String", _repo_name),
        "nameWithOwner": _field("String", _repo_name_with_owner),
        "owner": _field("User", _repo_owner),
        "defaultBranchRef": _field("Ref", _repo_default_branch_ref),
        "viewerPermission": _field("RepositoryPermission", _repo_viewer_permission),
        "hasIssuesEnabled": _field("Boolean", _repo_has_issues),
        "issue": _field("Issue", _repo_issue),
        "issues": _field("IssueConnection", _repo_issues),
        "pullRequests": _field("PullRequestConnection", _repo_pull_requests),
        "pullRequest": _field("PullRequest", _repo_pull_request),
        "description": _field("String", _repo_description),
        "isPrivate": _field("Boolean", _repo_private),
        "url": _field("URI", _repo_url),
        "createdAt": _field("DateTime", _repo_created_at),
        "updatedAt": _field("DateTime", _repo_updated_at),
    },
    "Issue": {
        "id": _field("ID", _issue_id),
        "number": _field("Int", _issue_number),
        "title": _field("String", _issue_title),
        "state": _field("IssueState", _issue_state),
        "body": _field("String", _issue_body),
        "url": _field("URI", lambda o, a, c: "%s/%s/%s/issues/%d" % (
            c.get("base_url", ""), c["login"], o["repo"].name, o["issue"].number)),
        "createdAt": _field("DateTime", _issue_created_at),
        "updatedAt": _field("DateTime", _issue_updated_at),
        "closedAt": _field("DateTime", _issue_closed_at),
        "author": _field("User", _issue_author),
        "labels": _field("LabelConnection", _issue_labels),
        "comments": _field("IssueCommentConnection", _issue_comments),
        "repository": _field("Repository", _issue_repository),
    },
    "PullRequest": {
        "id": _field("ID", _pull_id),
        "number": _field("Int", _pull_number),
        "headRefName": _field("String", _pull_head_ref),
        "baseRefName": _field("String", _pull_base_ref),
        "mergeable": _field("MergeableState", _pull_mergeable),
        "state": _field("PullRequestState", _pull_state),
        "merged": _field("Boolean", _pull_merged),
        "files": _field("PullRequestChangedFileConnection", _pull_files),
        "title": _field("String", _pull_title),
        "body": _field("String", _pull_body),
        "author": _field("User", _pull_author),
        "createdAt": _field("DateTime", _pull_created_at),
        "updatedAt": _field("DateTime", _pull_updated_at),
        "closedAt": _field("DateTime", _pull_closed_at),
        "mergedAt": _field("DateTime", _pull_merged_at),
        "mergeCommit": _field("Commit", _pull_merge_commit),
        "url": _field("URI", _pull_url),
        "repository": _field("Repository", _pull_repository),
        "headRefOid": _field("GitObjectID", _pull_head_oid),
        "baseRefOid": _field("GitObjectID", _pull_base_oid),
    },
    "Ref": {
        "name": _field("String", _ref_name),
        "target": _field("Commit", _ref_target),
        "prefix": _field("String", lambda o, a, c: "refs/heads/"),
    },
    "Commit": {
        "oid": _field("GitObjectID", _commit_oid),
        "abbreviatedOid": _field("String", lambda o, a, c: (o.get("oid") or "")[:7]),
        "id": _field("ID", lambda o, a, c: gid("Commit", abs(hash(o.get("oid") or "")) % 1000000000)),
    },
    "Label": {
        "name": _field("String", _label_name),
        "color": _field("String", _label_color),
        "description": _field("String", lambda o, a, c: o.get("description") or ""),
    },
    "IssueComment": {
        "id": _field("ID", _comment_id),
        "body": _field("String", _comment_body),
        "databaseId": _field("Int", lambda o, a, c: int(getattr(o.get("comment"), "id", 0) or 0)),
        "createdAt": _field("DateTime", lambda o, a, c: getattr(o.get("comment"), "created_at", None)),
        "updatedAt": _field("DateTime", lambda o, a, c: getattr(o.get("comment"), "updated_at", None)),
    },
    "IssueConnection": {
        "nodes": _field("Issue", lambda o, a, c: o.get("nodes") or []),
        "totalCount": _field("Int", lambda o, a, c: int(o.get("totalCount") or 0)),
        "pageInfo": _field("PageInfo", lambda o, a, c: o.get("pageInfo")),
    },
    "PullRequestConnection": {
        "nodes": _field("PullRequest", lambda o, a, c: o.get("nodes") or []),
        "totalCount": _field("Int", lambda o, a, c: int(o.get("totalCount") or 0)),
        "pageInfo": _field("PageInfo", lambda o, a, c: o.get("pageInfo")),
    },
    "IssueCommentConnection": {
        "nodes": _field("IssueComment", lambda o, a, c: o.get("nodes") or []),
        "totalCount": _field("Int", lambda o, a, c: int(o.get("totalCount") or 0)),
        "pageInfo": _field("PageInfo", lambda o, a, c: o.get("pageInfo")),
    },
    "LabelConnection": {
        "nodes": _field("Label", lambda o, a, c: o.get("nodes") or []),
        "totalCount": _field("Int", lambda o, a, c: int(o.get("totalCount") or 0)),
        "pageInfo": _field("PageInfo", lambda o, a, c: o.get("pageInfo")),
    },
    "PullRequestChangedFileConnection": {
        "nodes": _field("PullRequestChangedFile", lambda o, a, c: o.get("nodes") or []),
        "totalCount": _field("Int", lambda o, a, c: int(o.get("totalCount") or 0)),
        "pageInfo": _field("PageInfo", lambda o, a, c: o.get("pageInfo")),
    },
    "PullRequestChangedFile": {
        "path": _field("String", lambda o, a, c: o.get("path") or ""),
        "additions": _field("Int", lambda o, a, c: 0),
        "deletions": _field("Int", lambda o, a, c: 0),
    },
    "PageInfo": {
        "hasNextPage": _field("Boolean", lambda o, a, c: bool(o.get("hasNextPage"))),
        "hasPreviousPage": _field("Boolean", lambda o, a, c: bool(o.get("hasPreviousPage"))),
        "startCursor": _field("String", lambda o, a, c: o.get("startCursor")),
        "endCursor": _field("String", lambda o, a, c: o.get("endCursor")),
    },
    "CreateIssuePayload": {
        "issue": _field("Issue", lambda o, a, c: o.get("issue")),
        "clientMutationId": _field("String", lambda o, a, c: None),
    },
    "CloseIssuePayload": {
        "issue": _field("Issue", lambda o, a, c: o.get("issue")),
        "clientMutationId": _field("String", lambda o, a, c: None),
    },
    "CreatePullRequestPayload": {
        "pullRequest": _field("PullRequest", lambda o, a, c: o.get("pullRequest")),
        "clientMutationId": _field("String", lambda o, a, c: None),
    },
    "MergePullRequestPayload": {
        "pullRequest": _field("PullRequest", lambda o, a, c: o.get("pullRequest")),
        "clientMutationId": _field("String", lambda o, a, c: None),
    },
    "AddCommentPayload": {
        "commentEdge": _field("IssueCommentEdge", lambda o, a, c: o.get("commentEdge")),
        "clientMutationId": _field("String", lambda o, a, c: None),
    },
    "IssueCommentEdge": {
        "node": _field("IssueComment", lambda o, a, c: o.get("node")),
        "cursor": _field("String", lambda o, a, c: None),
    },
    "__Type": {
        "name": _field("String", _named),
        "kind": _field("__TypeKind", lambda o, a, c: "ENUM" if o.get("name") in ENUM_VALUES else "OBJECT"),
        "fields": _field("__Field", _type_fields),
        "enumValues": _field("__EnumValue", _type_enum_values),
        "description": _field("String", lambda o, a, c: None),
    },
    "__Field": {
        "name": _field("String", _field_name),
        "description": _field("String", lambda o, a, c: None),
        "isDeprecated": _field("Boolean", lambda o, a, c: False),
        "deprecationReason": _field("String", lambda o, a, c: None),
    },
    "__EnumValue": {
        "name": _field("String", _field_name),
        "description": _field("String", lambda o, a, c: None),
        "isDeprecated": _field("Boolean", lambda o, a, c: False),
        "deprecationReason": _field("String", lambda o, a, c: None),
    },
}


ENUM_VALUES: Dict[str, List[str]] = {
    "SearchType": ["ISSUE", "REPOSITORY", "USER", "PULL_REQUEST", "DISCUSSION"],
    "IssueState": ["OPEN", "CLOSED"],
    "PullRequestState": ["OPEN", "CLOSED", "MERGED"],
    "MergeableState": ["MERGEABLE", "CONFLICTING", "UNKNOWN"],
    "PullRequestMergeMethod": ["MERGE", "SQUASH", "REBASE"],
    "RepositoryPermission": ["ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ"],
    "IssueOrderField": ["CREATED_AT", "UPDATED_AT", "COMMENTS"],
    "OrderDirection": ["ASC", "DESC"],
    "IssueCommentOrderField": ["UPDATED_AT"],
}

FIELD_LISTS: Dict[str, List[str]] = {
    name: list(fields.keys()) for name, fields in SCHEMA.items()
}


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

def _type_matches(condition: Optional[str], runtime: str) -> bool:
    if not condition:
        return True
    if condition == runtime:
        return True
    # The only abstract type the suite uses is Node, which every object satisfies.
    if condition == "Node" and runtime in SCHEMA and runtime not in ("Query", "Mutation"):
        return True
    return False


def _complete(ctx: Dict[str, Any], declared_type: str, value: Any,
              selections: List[Dict[str, Any]], fragments: Dict[str, Any],
              variables: Dict[str, Any], errors: List[Dict[str, Any]],
              path: List[Any]) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        return [
            _complete_one(ctx, declared_type, item, selections, fragments, variables, errors, path + [index])
            for index, item in enumerate(value)
        ]
    return _complete_one(ctx, declared_type, value, selections, fragments, variables, errors, path)


def _complete_one(ctx: Dict[str, Any], declared_type: str, value: Any,
                  selections: List[Dict[str, Any]], fragments: Dict[str, Any],
                  variables: Dict[str, Any], errors: List[Dict[str, Any]],
                  path: List[Any]) -> Any:
    if not isinstance(value, dict):
        return value
    runtime = value.get("__typename") or declared_type
    return _execute_selections(ctx, runtime, value, selections, fragments, variables, errors, path)


def _execute_selections(ctx: Dict[str, Any], type_name: str, obj: Dict[str, Any],
                        selections: List[Dict[str, Any]], fragments: Dict[str, Any],
                        variables: Dict[str, Any], errors: List[Dict[str, Any]],
                        path: List[Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    runtime = obj.get("__typename") or type_name
    type_fields = SCHEMA.get(runtime) or SCHEMA.get(type_name) or {}

    for selection in selections:
        kind = selection.get("kind")
        if kind == "field":
            key = selection.get("alias") or selection.get("name")
            name = selection.get("name")
            if name == "__typename":
                result[key] = runtime
                continue
            definition = type_fields.get(name)
            if definition is None:
                errors.append({
                    "message": "Field '%s' doesn't exist on type '%s'" % (name, runtime),
                    "path": list(path) + [key],
                })
                result[key] = None
                continue
            return_type, resolver = definition
            args = _resolve_args(selection.get("args") or {}, variables)
            try:
                value = resolver(obj, args, ctx)
            except GraphQLExecutionError as exc:
                errors.append({"message": str(exc), "path": list(path) + [key]})
                result[key] = None
                continue
            except Exception as exc:  # never let one field kill the document
                errors.append({"message": "%s: %s" % (type(exc).__name__, exc),
                               "path": list(path) + [key]})
                result[key] = None
                continue
            if selection.get("selections"):
                result[key] = _complete(ctx, return_type, value, selection["selections"],
                                        fragments, variables, errors, list(path) + [key])
            else:
                result[key] = value
        elif kind == "inline":
            condition = selection.get("type")
            if not _type_matches(condition, runtime):
                continue
            result.update(_execute_selections(ctx, runtime, obj, selection.get("selections") or [],
                                              fragments, variables, errors, path))
        elif kind == "spread":
            fragment = fragments.get(selection.get("name"))
            if fragment is None or not _type_matches(fragment.get("type"), runtime):
                continue
            result.update(_execute_selections(ctx, runtime, obj, fragment.get("selections") or [],
                                              fragments, variables, errors, path))
    return result


def _execute_operation(store: Any, document: Dict[str, Any], operation: Dict[str, Any],
                       variables: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    errors: List[Dict[str, Any]] = []
    # Apply variable defaults for variables the caller omitted.
    merged_vars = dict(variables or {})
    for definition in operation.get("variables") or []:
        name = definition.get("name")
        if name not in merged_vars and definition.get("default") is not None:
            merged_vars[name] = _resolve_value(definition["default"], merged_vars)

    root_type = "Mutation" if operation.get("op") == "mutation" else "Query"
    ctx = {
        "store": store,
        "login": store.get_owner_login(),
        "base_url": base_url,
    }
    root_obj = {"__typename": root_type}
    data = _execute_selections(ctx, root_type, root_obj, operation.get("selections") or [],
                               document.get("fragments") or {}, merged_vars, errors, [])
    result: Dict[str, Any] = {"data": data}
    if errors:
        result["errors"] = errors
    return result


def execute(store: Any, query: str, variables: Optional[Dict[str, Any]] = None,
            base_url: str = "", operation_name: Optional[str] = None) -> Dict[str, Any]:
    """Parse and execute one GraphQL document, answering a {data, errors?} object."""
    try:
        document = parse(query)
    except GraphQLSyntaxError as exc:
        return {"data": None, "errors": [{"message": "Syntax error: %s" % exc}]}
    operations = document.get("operations") or []
    if not operations:
        return {"data": None, "errors": [{"message": "No operation found in the document"}]}
    operation = operations[0]
    if operation_name:
        for candidate in operations:
            if candidate.get("name") == operation_name:
                operation = candidate
                break
    with store.lock:
        try:
            return _execute_operation(store, document, operation, variables or {}, base_url)
        except Exception as exc:
            return {"data": None, "errors": [{"message": "%s: %s" % (type(exc).__name__, exc)}]}


# ---------------------------------------------------------------------------
# HTTP entry point
# ---------------------------------------------------------------------------

def _execute_request(store: Any, request: Any, base_url: str) -> Dict[str, Any]:
    if not isinstance(request, dict):
        return {"data": None, "errors": [{"message": "The request body must be a JSON object"}]}
    query = request.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"data": None, "errors": [{"message": "Must provide a query string"}]}
    variables = request.get("variables")
    if not isinstance(variables, dict):
        variables = {}
    return execute(store, query, variables, base_url, request.get("operationName"))


def handle_graphql(store: Any, body: Any, base_url: str = "") -> Tuple[int, Any]:
    """Handle POST /api/graphql: answer (status, JSON-able payload).

    ``body`` may be raw bytes/str JSON or an already-decoded object.  A JSON
    array is GitHub-style operation batching and answers an array of results.
    """
    try:
        if isinstance(body, (bytes, bytearray)):
            payload = json.loads(body.decode("utf-8"))
        elif isinstance(body, str):
            payload = json.loads(body)
        else:
            payload = body
    except Exception:
        return 400, {"message": "Problems parsing JSON"}

    if isinstance(payload, list):
        return 200, [_execute_request(store, item, base_url) for item in payload]
    if not isinstance(payload, dict):
        return 400, {"message": "Problems parsing JSON"}
    return 200, _execute_request(store, payload, base_url)


# Convenience aliases for the HTTP layer.
graphql = handle_graphql
handle = handle_graphql
