"""upload-pack (fetch) half of git smart HTTP: protocol v2 and protocol v0.

``handle_upload_pack(store, owner, repo_name, body, git_protocol_header)``
answers ``POST /{owner}/{repo}.git/git-upload-pack``.  A body whose first
pkt-line starts ``command=`` is a v2 request (``command=fetch``); anything else
is the v0 ``want``/``have``/``done`` negotiation.  Both shapes reply with a
sideband-framed packfile, and v2 additionally carries ``acknowledgments`` and
(only when a deepen/depth applies) ``shallow-info`` sections.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from . import pktline
from .objects import Commit, Tag, Tree
from .packfile import build_pack, walk_closure

__all__ = ["handle_upload_pack"]

ZERO_OID = "0" * 40
RESULT_TYPE = "application/x-git-upload-pack-result"
TEXT_TYPE = "text/plain; charset=utf-8"

_NOT_FOUND = (404, TEXT_TYPE, b"repository not found\n")
_BAD_REQUEST = (400, TEXT_TYPE, b"malformed request\n")
_UNSUPPORTED = (400, TEXT_TYPE, b"unsupported command\n")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _is_oid(text: str) -> bool:
    if len(text) != 40:
        return False
    try:
        int(text, 16)
    except ValueError:
        return False
    return True


def _text(line: Any) -> Optional[str]:
    """Decode a pkt-line payload; ``None`` for control packets."""
    if not isinstance(line, bytes):
        return None
    return line.decode("utf-8", "replace").rstrip("\n")


def _new_args() -> Dict[str, Any]:
    return {
        "want": [],
        "have": [],
        "shallow": [],
        "deepen": None,
        "deepen_since": None,
        "deepen_not": [],
        "done": False,
        "wait_for_done": False,
        "caps": [],
    }


def _parse_args(body: bytes) -> Tuple[Optional[str], Dict[str, Any]]:
    """Classify the body and collect its argument lines.

    Returns ``(kind, args)`` where ``kind`` is ``"v2"``, ``"v0"``, the v2
    command string when it is not ``fetch``, or ``None`` when the body is not
    pkt-line framed at all.
    """
    reader = pktline.PktReader(body)
    first = reader.read_line()
    if not isinstance(first, bytes):
        return None, _new_args()
    head = first.decode("utf-8", "replace").rstrip("\n")

    args = _new_args()
    if head.startswith("command="):
        if head != "command=fetch":
            return head, args
        kind = "v2"
    else:
        kind = "v0"

    first_want = True
    while True:
        line = reader.read_line()
        if line is None:
            break
        text = _text(line)
        if text is None:
            continue
        if text.startswith("want "):
            parts = text.split(" ")
            oid = parts[1] if len(parts) > 1 else ""
            if _is_oid(oid):
                args["want"].append(oid)
            if first_want and len(parts) > 2:
                args["caps"].extend(parts[2:])
            first_want = False
        elif text.startswith("have "):
            oid = text[5:].strip()
            if _is_oid(oid):
                args["have"].append(oid)
        elif text.startswith("shallow "):
            oid = text[8:].strip()
            if _is_oid(oid):
                args["shallow"].append(oid)
        elif text.startswith("deepen-since "):
            try:
                args["deepen_since"] = int(text[13:].strip())
            except ValueError:
                pass
        elif text.startswith("deepen-not "):
            args["deepen_not"].append(text[11:].strip())
        elif text.startswith("deepen "):
            try:
                args["deepen"] = int(text[7:].strip())
            except ValueError:
                pass
        elif text == "done":
            args["done"] = True
        elif text == "wait-for-done":
            args["wait_for_done"] = True
        # thin-pack / ofs-delta / no-progress / filter / agent / object-format
        # need no handling: we always send complete, non-delta objects.
    return kind, args


def _peel(store: Any, oid: str) -> Tuple[Optional[str], Optional[bytes], str]:
    """Follow annotated-tag chains; returns (type, payload, peeled oid)."""
    seen: Set[str] = set()
    while oid not in seen:
        seen.add(oid)
        try:
            obj_type, payload = store.get(oid)
        except KeyError:
            return None, None, oid
        if obj_type != "tag":
            return obj_type, payload, oid
        oid = Tag.parse(payload).object
    return None, None, oid


def _commit_parents(store: Any, oid: str) -> List[str]:
    obj_type, payload, _ = _peel(store, oid)
    if obj_type != "commit" or payload is None:
        return []
    return list(Commit.parse(payload).parents)


def _tree_closure(store: Any, oid: str) -> Set[str]:
    """Objects a client that holds ``oid`` certainly also holds.

    Walks the commit's tree (never its parents), so a shallow boundary's
    ancestry is *not* treated as held.
    """
    out: Set[str] = set()
    stack = [oid]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        try:
            obj_type, payload = store.get(cur)
        except KeyError:
            continue
        out.add(cur)
        if obj_type == "tree":
            for entry in Tree.parse(payload).entries:
                if entry.oid not in out:
                    stack.append(entry.oid)
        elif obj_type == "commit":
            tree_oid = Commit.parse(payload).tree
            if tree_oid not in out:
                stack.append(tree_oid)
        elif obj_type == "tag":
            target = Tag.parse(payload).object
            if target not in out:
                stack.append(target)
    return out


def _pack_count(pack: bytes) -> int:
    if len(pack) < 12:
        return 0
    return int.from_bytes(pack[8:12], "big")


# ---------------------------------------------------------------------------
# object selection (shared by both protocols)
# ---------------------------------------------------------------------------

def _select_pack(
    store: Any,
    repo: Any,
    wants: List[str],
    haves: List[str],
    shallow: List[str],
    depth: Optional[int],
    has_deepen: bool,
) -> Tuple[bytes, List[str], int]:
    """Return ``(pack bytes, shallow-info lines, object count)``.

    Selection is ``closure(wants) - closure(haves)`` under the depth rule of
    ``walk_closure``; client-supplied shallow oids are treated as extra
    have-roots whose ancestry is cut (so a deepen delivers exactly the next
    generation).
    """
    key = (repo.name, tuple(sorted(wants)), depth, tuple(sorted(haves)))
    use_cache = not shallow
    cached = store.get_pack_cache(key) if use_cache else None
    if cached is not None and not has_deepen:
        return cached, [], _pack_count(cached)

    cut = set(shallow)
    exclude = {h for h in haves if h not in cut}
    included, boundary = walk_closure(store, wants, exclude=exclude, depth=depth)

    held: Set[str] = set()
    stack = list(set(haves))
    while stack:
        cur = stack.pop()
        if cur in held:
            continue
        try:
            obj_type, payload = store.get(cur)
        except KeyError:
            continue
        held.add(cur)
        if obj_type == "tree":
            for entry in Tree.parse(payload).entries:
                stack.append(entry.oid)
        elif obj_type == "commit":
            commit = Commit.parse(payload)
            stack.append(commit.tree)
            if cur not in cut:
                stack.extend(commit.parents)
        elif obj_type == "tag":
            stack.append(Tag.parse(payload).object)

    shallow_lines: List[str] = []
    if has_deepen:
        for oid in sorted(boundary):
            if oid in included and oid not in held and store.has(oid):
                shallow_lines.append("shallow " + oid)
        for oid in sorted(cut):
            if oid not in included or oid in held:
                continue
            parents = _commit_parents(store, oid)
            if parents and all(p in included and p not in held for p in parents):
                shallow_lines.append("unshallow " + oid)

    if cached is not None:
        return cached, shallow_lines, _pack_count(cached)

    oids = sorted(o for o in included if o not in held and store.has(o))
    objects: List[Tuple[str, bytes]] = []
    for oid in oids:
        try:
            objects.append(store.get(oid))
        except KeyError:
            continue
    pack = build_pack(objects)
    if use_cache:
        store.set_pack_cache(key, pack)
    return pack, shallow_lines, len(objects)


# ---------------------------------------------------------------------------
# reply builders
# ---------------------------------------------------------------------------

def _v2_reply(ack_lines: List[str], shallow_lines: List[str], pack: bytes,
              send_pack: bool) -> bytes:
    out = bytearray()
    out.extend(pktline.pkt(b"acknowledgments\n"))
    for line in ack_lines:
        out.extend(pktline.pkt(line.encode("ascii") + b"\n"))
    if not send_pack:
        out.extend(pktline.flush())
        return bytes(out)

    out.extend(pktline.delim())
    if shallow_lines:
        out.extend(pktline.pkt(b"shallow-info\n"))
        for line in shallow_lines:
            out.extend(pktline.pkt(line.encode("ascii") + b"\n"))
        out.extend(pktline.delim())

    out.extend(pktline.pkt(b"packfile\n"))
    out.extend(pktline.sideband(1, pack))
    out.extend(pktline.flush())
    return bytes(out)


def _v0_reply(shallow_lines: List[str], ack_line: str, pack: bytes) -> bytes:
    out = bytearray()
    for line in shallow_lines:
        out.extend(pktline.pkt(line.encode("ascii") + b"\n"))
    out.extend(pktline.pkt(ack_line.encode("ascii") + b"\n"))
    out.extend(pktline.sideband(1, pack))
    out.extend(pktline.flush())
    return bytes(out)


# ---------------------------------------------------------------------------
# protocol handlers
# ---------------------------------------------------------------------------

def _handle_v2(store: Any, repo: Any, args: Dict[str, Any]) -> Tuple[int, str, bytes]:
    wants = list(dict.fromkeys(args["want"]))
    haves = list(dict.fromkeys(args["have"]))
    shallow = list(dict.fromkeys(args["shallow"]))
    depth = args["deepen"]
    has_deepen = (
        depth is not None
        or args["deepen_since"] is not None
        or bool(args["deepen_not"])
    )

    ack: Optional[str] = None
    for have in haves:
        if store.has(have):
            ack = have
            break
    ack_line = ("ACK " + ack) if ack is not None else "NAK"

    if not wants:
        # Nothing requested: still frame a valid (empty) packfile section.
        body = _v2_reply([ack_line], [], build_pack([]), True)
        return 200, RESULT_TYPE, body

    pack, shallow_lines, _count = _select_pack(
        store, repo, wants, haves, shallow, depth, has_deepen
    )

    if args["done"] or args["wait_for_done"]:
        body = _v2_reply([ack_line], shallow_lines, pack, True)
    else:
        # Negotiation only: acknowledgments, no packfile section yet.
        body = _v2_reply([ack_line], [], pack, False)
    return 200, RESULT_TYPE, body


def _handle_v0(store: Any, repo: Any, args: Dict[str, Any]) -> Tuple[int, str, bytes]:
    wants = list(dict.fromkeys(args["want"]))
    haves = list(dict.fromkeys(args["have"]))
    shallow = list(dict.fromkeys(args["shallow"]))
    depth = args["deepen"]
    has_deepen = (
        depth is not None
        or args["deepen_since"] is not None
        or bool(args["deepen_not"])
    )

    if not wants:
        return 200, RESULT_TYPE, _v0_reply([], "NAK", build_pack([]))

    pack, shallow_lines, count = _select_pack(
        store, repo, wants, haves, shallow, depth, has_deepen
    )

    if count == 0:
        held_have: Optional[str] = None
        for have in haves:
            if store.has(have):
                held_have = have
                break
        ack_line = ("ACK " + held_have) if held_have is not None else "NAK"
    else:
        ack_line = "NAK"

    return 200, RESULT_TYPE, _v0_reply(shallow_lines, ack_line, pack)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def handle_upload_pack(store: Any, owner: str, repo_name: str, body: bytes,
                       git_protocol_header: Optional[str] = None
                       ) -> Tuple[int, str, bytes]:
    """Answer a ``git-upload-pack`` POST for one repo.

    ``git_protocol_header`` is accepted for symmetry with the advertisement
    handler; the request body alone decides v2 versus v0.
    """
    try:
        if store.bind_owner(owner) != owner:
            return _NOT_FOUND
        repo = store.get_repo(repo_name)
        if repo is None:
            return _NOT_FOUND
        if not body:
            return _BAD_REQUEST

        kind, args = _parse_args(body)
        if kind is None:
            return _BAD_REQUEST
        if kind == "v2":
            return _handle_v2(store, repo, args)
        if kind == "v0":
            return _handle_v0(store, repo, args)
        return _UNSUPPORTED
    except ValueError:
        return _BAD_REQUEST
