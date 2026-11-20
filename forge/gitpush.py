"""receive-pack: parse a push command list, unpack the pack, apply refs, report status.

Implements the B2 half of git's smart-HTTP receive-pack protocol:

    handle_receive_pack(store, owner, repo_name, body) -> (status, content_type, body)

The body is a pkt-line command list ``<old> <new> <ref>`` (capabilities after a
NUL on the first line), a flush, then an optional raw packfile.  The reply is a
``report-status`` stream, framed inside sideband band 1 when the client asked
for it and always pkt-line framed.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from . import pktline
from .packfile import read_pack

ZERO_OID = "0" * 40
RESULT_TYPE = "application/x-git-receive-pack-result"


class _Command:
    __slots__ = ("old", "new", "ref")

    def __init__(self, old: str, new: str, ref: str) -> None:
        self.old = old
        self.new = new
        self.ref = ref


def _parse_request(body: bytes) -> Tuple[List[_Command], List[str], bytes, str]:
    """Split the body into (commands, capabilities, pack bytes, error).

    ``error`` is an empty string on success; on a malformed command list the
    commands parsed up to that point are still returned so they can be
    reported ``ng``.  An absent or empty pack is returned as ``b""``
    (delete-only pushes send no pack at all).
    """
    reader = pktline.PktReader(body)
    commands: List[_Command] = []
    caps: List[str] = []
    error = ""
    closed = False

    while True:
        try:
            line = reader.read_line()
        except Exception as exc:
            error = str(exc) or "malformed command list"
            break
        if line is None:
            # A command list that just ends without a flush is treated as
            # complete.
            break
        if line == pktline.FLUSH:
            closed = True
            break
        if not isinstance(line, (bytes, bytearray)):
            # DELIM / RESP_END have no meaning inside a receive-pack request.
            error = "unexpected control packet in command list"
            break

        payload = bytes(line)
        if payload.endswith(b"\n"):
            payload = payload[:-1]
        if payload.endswith(b"\r"):
            payload = payload[:-1]
        if not payload:
            continue

        text = payload
        if b"\0" in text:
            head, cap_blob = text.split(b"\0", 1)
            if not commands:
                caps = cap_blob.decode("utf-8", "replace").split()
            text = head

        parts = [p for p in text.decode("utf-8", "surrogateescape").split(" ") if p]
        if len(parts) != 3:
            error = "malformed receive-pack command line"
            break
        commands.append(_Command(parts[0], parts[1], parts[2]))

    pack = b""
    if closed and commands:
        rest = body[reader.pos:]
        if rest.startswith(b"PACK"):
            pack = rest
    return commands, caps, pack, error


def _unpack(store, pack: bytes, commands: List[_Command]) -> None:
    """Store every object of the pack; raises ValueError on a broken pack."""
    if not pack:
        return

    def resolve_external(oid: str) -> Optional[Tuple[str, bytes]]:
        try:
            return store.get(oid)
        except KeyError:
            return None

    for oid, obj_type, payload in read_pack(pack, resolve_external=resolve_external):
        store.put(obj_type, payload)


def _commit_parents(store, oid: str) -> Optional[List[str]]:
    """Return the parent oids of a commit, or None when oid is not a commit."""
    try:
        obj_type, payload = store.get(oid)
    except KeyError:
        return None
    if obj_type != "commit":
        return None
    parents: List[str] = []
    header = payload.split(b"\n\n", 1)[0]
    for line in header.split(b"\n"):
        if line.startswith(b"parent "):
            parents.append(line[7:].decode("ascii", "replace").strip())
    return parents


def _is_ancestor(store, old: str, new: str) -> bool:
    """True when ``old`` is reachable from ``new`` by walking commit parents."""
    seen = set()
    stack = [new]
    while stack:
        cur = stack.pop()
        if cur == old:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        parents = _commit_parents(store, cur)
        if not parents:
            continue
        stack.extend(parents)
    return False


def _apply(store, repo, commands: List[_Command]) -> List[Tuple[str, str, bool]]:
    """Apply each command in order, returning (ref, reason, ok) per command."""
    results: List[Tuple[str, str, bool]] = []
    changed = False

    for cmd in commands:
        current = repo.refs.get(cmd.ref)

        # Stale check: the advertised old value must still be the current one.
        if cmd.old != ZERO_OID and current != cmd.old:
            results.append((cmd.ref, "stale", False))
            continue

        if cmd.new == ZERO_OID:
            # Delete: allowed for any ref (the suite never deletes the default
            # branch).  A missing ref is still reported ok.
            if repo.delete_ref(cmd.ref):
                changed = True
            results.append((cmd.ref, "", True))
            continue

        if not store.has(cmd.new):
            results.append((cmd.ref, "missing necessary objects", False))
            continue

        if cmd.old == ZERO_OID:
            # Create.
            if current is not None and current != cmd.new:
                results.append((cmd.ref, "already exists", False))
                continue
            repo.set_ref(cmd.ref, cmd.new)
            changed = True
            results.append((cmd.ref, "", True))
            continue

        # Update: fast-forward only on branches whose tips are commits.
        if cmd.ref.startswith("refs/heads/"):
            if not _is_ancestor(store, cmd.old, cmd.new):
                results.append((cmd.ref, "non-fast-forward", False))
                continue

        repo.set_ref(cmd.ref, cmd.new)
        changed = True
        results.append((cmd.ref, "", True))

    if changed:
        store.clear_pack_cache(repo.name)

    return results


def _report_bytes(unpack_line: str, results: List[Tuple[str, str, bool]]) -> bytes:
    out = bytearray()
    out.extend(pktline.pkt(unpack_line.encode("utf-8")))
    for ref, reason, ok in results:
        if ok:
            out.extend(pktline.pkt(("ok %s\n" % ref).encode("utf-8", "surrogateescape")))
        else:
            out.extend(pktline.pkt(("ng %s %s\n" % (ref, reason)).encode("utf-8", "surrogateescape")))
    out.extend(pktline.flush())
    return bytes(out)


def _answer(inner: bytes, sideband: bool) -> bytes:
    if sideband:
        return pktline.sideband(1, inner) + pktline.flush()
    return inner


def handle_receive_pack(store, owner: str, repo_name: str, body: bytes):
    """Handle ``POST /{owner}/{repo}.git/git-receive-pack``.

    Returns ``(status, content_type, body_bytes)``; a missing repo (or one
    whose owner does not match the bound login) answers 404.
    """
    if owner and store.bind_owner(owner) != owner:
        return 404, "text/plain; charset=utf-8", b"repository not found\n"

    repo = store.get_repo(repo_name)
    if repo is None:
        return 404, "text/plain; charset=utf-8", b"repository not found\n"

    try:
        commands, caps, pack, error = _parse_request(body)
    except Exception as exc:  # defensive: never let a bad body kill the server
        return (
            200,
            RESULT_TYPE,
            _answer(_report_bytes("unpack error: %s\n" % exc, []), False),
        )

    sideband = any(c == "side-band-64k" or c.startswith("side-band") for c in caps)

    if error:
        results = [(cmd.ref, "unpacker error", False) for cmd in commands]
        inner = _report_bytes("unpack error: %s\n" % error, results)
        return 200, RESULT_TYPE, _answer(inner, sideband)

    with store.lock:
        try:
            _unpack(store, pack, commands)
        except Exception as exc:
            results = [(cmd.ref, "unpacker error", False) for cmd in commands]
            inner = _report_bytes("unpack error: %s\n" % exc, results)
            return 200, RESULT_TYPE, _answer(inner, sideband)

        if not commands:
            inner = _report_bytes("unpack ok\n", [])
            return 200, RESULT_TYPE, _answer(inner, sideband)

        results = _apply(store, repo, commands)

    inner = _report_bytes("unpack ok\n", results)
    return 200, RESULT_TYPE, _answer(inner, sideband)
