"""Ref advertisement (``GET info/refs``) and protocol-v2 ``ls-refs``.

Two public entry points, both returning ``(status, content_type, body_bytes)``:

``handle_info_refs(store, owner, repo_name, service, git_protocol_header)``
    Serves the v2 capability advertisement when the request header
    ``Git-Protocol`` asks for ``version=2`` and the service is
    ``git-upload-pack``; otherwise the v0 ref advertisement (service preamble,
    flush, then ``<oid> <ref>`` lines with capabilities after a NUL on the
    first line).

``handle_ls_refs(store, owner, repo_name, body)``
    Answers a ``command=ls-refs`` v2 request: parses ``symrefs``, ``peel``,
    ``unborn`` and ``ref-prefix <prefix>`` arguments and replies with the
    sectionless ref list followed by a flush.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Sequence, Tuple

from . import pktline
from .objects import Tag

__all__ = ["handle_info_refs", "handle_ls_refs"]

ZERO_OID = "0" * 40
AGENT = "forge/1.0"

UPLOAD = "git-upload-pack"
RECEIVE = "git-receive-pack"

UPLOAD_ADV = "application/x-git-upload-pack-advertisement"
RECEIVE_ADV = "application/x-git-receive-pack-advertisement"
UPLOAD_RESULT = "application/x-git-upload-pack-result"
TEXT_TYPE = "text/plain; charset=utf-8"

UPLOAD_CAPS = "side-band-64k ofs-delta agent=" + AGENT
RECEIVE_CAPS = "report-status delete-refs side-band-64k ofs-delta agent=" + AGENT


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _not_found() -> Tuple[int, str, bytes]:
    return (404, TEXT_TYPE, b"repository not found\n")


def _bad_request(message: str = "malformed request\n") -> Tuple[int, str, bytes]:
    return (400, TEXT_TYPE, message.encode("utf-8"))


def _repo_for(store: Any, owner: str, repo_name: str) -> Optional[Any]:
    """Bind the owner from the path and return the repo, or ``None`` for 404."""
    if not owner or not repo_name:
        return None
    store.bind_owner(owner)
    if store.get_owner_login() != owner:
        return None
    return store.get_repo(repo_name)


def _line_text(payload: bytes) -> str:
    return payload.decode("utf-8", "replace").rstrip("\n")


def _wants_v2(git_protocol_header: Optional[str]) -> bool:
    if not git_protocol_header:
        return False
    for token in re.split(r"[:,\s]+", str(git_protocol_header)):
        if token == "version=2":
            return True
    return False


def _head_wanted(prefixes: Sequence[str], target: str) -> bool:
    """Whether HEAD belongs in an ls-refs reply for these prefixes.

    With no prefixes HEAD is always sent.  A prefix that is a prefix of the
    literal name ``HEAD`` (notably ``""`` and ``"HEAD"``) also matches.  A
    prefix that stops at or above the ``refs/`` namespace (e.g. ``refs/``)
    matches through HEAD's symref target, which is what clients that ask for
    ``ref-prefix refs/`` expect.
    """
    if not prefixes:
        return True
    for prefix in prefixes:
        if "HEAD".startswith(prefix):
            return True
        if target and target.startswith(prefix) and "refs/".startswith(prefix):
            return True
    return False


def _peel_oid(store: Any, oid: str) -> Optional[str]:
    """Oid an annotated tag ultimately points at, or ``None`` when not a tag."""
    seen = set()
    cur = oid
    is_tag = False
    while cur not in seen:
        seen.add(cur)
        try:
            obj_type, payload = store.get(cur)
        except KeyError:
            return cur if is_tag else None
        if obj_type != "tag":
            return cur if is_tag else None
        is_tag = True
        try:
            cur = Tag.parse(payload).object
        except Exception:
            return cur
    return cur if is_tag else None


# ---------------------------------------------------------------------------
# advertisements
# ---------------------------------------------------------------------------

def _v2_advertisement() -> bytes:
    out = bytearray()
    out += pktline.pkt(b"version 2\n")
    out += pktline.pkt(b"agent=" + AGENT.encode("ascii") + b"\n")
    out += pktline.pkt(b"ls-refs=unborn\n")
    out += pktline.pkt(b"fetch=shallow wait-for-done\n")
    out += pktline.pkt(b"server-option\n")
    out += pktline.pkt(b"object-format=sha1\n")
    out += pktline.flush()
    return bytes(out)


def _v0_advertisement(repo: Any, service: str) -> bytes:
    caps = UPLOAD_CAPS if service == UPLOAD else RECEIVE_CAPS
    out = bytearray()
    out += pktline.pkt(("# service=" + service + "\n").encode("ascii"))
    out += pktline.flush()

    refs = repo.refs
    target = "refs/heads/" + repo.default_branch
    tip = refs.get(target)

    if tip is not None:
        lines: List[Tuple[str, str]] = [(tip, "HEAD")]
    elif not refs:
        # Empty repository: the single pseudo-ref line.
        lines = [(ZERO_OID, "capabilities^{}")]
    else:
        lines = [(ZERO_OID, "HEAD")]

    for name in sorted(refs):
        lines.append((refs[name], name))

    for index, (oid, name) in enumerate(lines):
        payload = ("%s %s\n" % (oid, name)).encode("utf-8")
        if index == 0:
            payload = payload[:-1] + b"\x00" + caps.encode("ascii") + b"\n"
        out += pktline.pkt(payload)
    out += pktline.flush()
    return bytes(out)


def handle_info_refs(
    store: Any,
    owner: str,
    repo_name: str,
    service: str,
    git_protocol_header: Optional[str],
) -> Tuple[int, str, bytes]:
    """``GET /{owner}/{repo}.git/info/refs?service=...``."""
    if service not in (UPLOAD, RECEIVE):
        return _bad_request("unsupported service\n")
    repo = _repo_for(store, owner, repo_name)
    if repo is None:
        return _not_found()

    content_type = UPLOAD_ADV if service == UPLOAD else RECEIVE_ADV
    if service == UPLOAD and _wants_v2(git_protocol_header):
        return (200, content_type, _v2_advertisement())
    return (200, content_type, _v0_advertisement(repo, service))


# ---------------------------------------------------------------------------
# protocol v2 ls-refs
# ---------------------------------------------------------------------------

class _LsRefsRequest:
    __slots__ = ("symrefs", "peel", "unborn", "prefixes")

    def __init__(self) -> None:
        self.symrefs = False
        self.peel = False
        self.unborn = False
        self.prefixes: List[str] = []


def _parse_ls_refs(body: bytes) -> Optional[_LsRefsRequest]:
    try:
        reader = pktline.PktReader(body or b"")
        first = reader.read_line()
    except ValueError:
        return None
    if not isinstance(first, bytes):
        return None
    if _line_text(first) != "command=ls-refs":
        return None

    request = _LsRefsRequest()
    in_arguments = False
    while True:
        try:
            line = reader.read_line()
        except ValueError:
            return None
        if line is None:
            break
        if not isinstance(line, bytes):
            if line == pktline.DELIM:
                in_arguments = True
                continue
            if line == pktline.FLUSH:
                break
            continue
        text = _line_text(line)
        if not in_arguments:
            # Capability lines (agent=..., object-format=sha1) carry nothing
            # this handler needs.
            continue
        if text == "symrefs":
            request.symrefs = True
        elif text == "peel":
            request.peel = True
        elif text == "unborn":
            request.unborn = True
        elif text.startswith("ref-prefix"):
            rest = text[len("ref-prefix"):]
            request.prefixes.append(rest[1:] if rest.startswith(" ") else "")
    return request


def _ls_refs_reply(store: Any, repo: Any, request: _LsRefsRequest) -> bytes:
    prefixes = request.prefixes
    target = "refs/heads/" + repo.default_branch
    tip = repo.refs.get(target)
    out = bytearray()

    if _head_wanted(prefixes, target):
        if tip is not None:
            line = "%s HEAD" % tip
            if request.symrefs:
                line += " symref-target:" + target
            out += pktline.pkt((line + "\n").encode("utf-8"))
        elif request.unborn:
            line = "%s HEAD" % ZERO_OID
            if request.symrefs:
                line += " symref-target:" + target
            out += pktline.pkt((line + "\n").encode("utf-8"))
        # Unborn and `unborn` was not requested: omit HEAD entirely.

    for name in sorted(repo.refs):
        if prefixes and not any(name.startswith(p) for p in prefixes):
            continue
        line = "%s %s" % (repo.refs[name], name)
        if request.peel:
            peeled = _peel_oid(store, repo.refs[name])
            if peeled is not None:
                line += " peeled:" + peeled
        out += pktline.pkt((line + "\n").encode("utf-8"))

    out += pktline.flush()
    return bytes(out)


def handle_ls_refs(
    store: Any,
    owner: str,
    repo_name: str,
    body: bytes,
) -> Tuple[int, str, bytes]:
    """``POST /{owner}/{repo}.git/git-upload-pack`` with ``command=ls-refs``."""
    repo = _repo_for(store, owner, repo_name)
    if repo is None:
        return _not_found()
    request = _parse_ls_refs(body)
    if request is None:
        return _bad_request()
    return (200, UPLOAD_RESULT, _ls_refs_reply(store, repo, request))
