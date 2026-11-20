"""Git object representation, parsing, and serialization."""

import hashlib
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Union


def oid_of(obj_type: str, payload: bytes) -> str:
    """Compute SHA-1 hex OID for a git object payload."""
    header = f"{obj_type} {len(payload)}\x00".encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()


def format_ident(name: str, email: str, epoch: int) -> bytes:
    """Format identity as bytes: b"<name> <email> <epoch> +0000"."""
    return f"{name} <{email}> {epoch} +0000".encode("utf-8")


@dataclass
class Blob:
    data: bytes

    def serialize(self) -> bytes:
        return self.data

    @classmethod
    def parse(cls, payload: bytes) -> "Blob":
        return cls(payload)


@dataclass
class TreeEntry:
    mode: int
    name: str
    oid: str

    def __post_init__(self):
        if isinstance(self.mode, str):
            self.mode = int(self.mode, 8)

    def is_dir(self) -> bool:
        return (self.mode & 0o170000) == 0o040000


@dataclass
class Tree:
    entries: List[TreeEntry] = field(default_factory=list)

    def serialize(self) -> bytes:
        def _sort_key(entry: TreeEntry) -> bytes:
            name_bytes = entry.name.encode("utf-8")
            if (entry.mode & 0o170000) == 0o040000:
                return name_bytes + b"/"
            return name_bytes

        sorted_entries = sorted(self.entries, key=_sort_key)
        out = bytearray()
        for e in sorted_entries:
            mode_str = f"{e.mode:o}"
            out.extend(f"{mode_str} ".encode("ascii"))
            out.extend(e.name.encode("utf-8"))
            out.append(0)
            out.extend(bytes.fromhex(e.oid))
        return bytes(out)

    @classmethod
    def parse(cls, payload: bytes) -> "Tree":
        entries = []
        pos = 0
        length = len(payload)
        while pos < length:
            space_idx = payload.find(b" ", pos)
            if space_idx == -1:
                raise ValueError("malformed tree object: space not found")
            mode_bytes = payload[pos:space_idx]
            mode = int(mode_bytes.decode("ascii"), 8)

            nul_idx = payload.find(b"\x00", space_idx + 1)
            if nul_idx == -1:
                raise ValueError("malformed tree object: NUL not found")
            name_bytes = payload[space_idx + 1:nul_idx]
            name = name_bytes.decode("utf-8", errors="surrogateescape")

            if nul_idx + 1 + 20 > length:
                raise ValueError("malformed tree object: truncated OID")
            sha_bytes = payload[nul_idx + 1:nul_idx + 21]
            oid = sha_bytes.hex()
            pos = nul_idx + 21
            entries.append(TreeEntry(mode=mode, name=name, oid=oid))
        return cls(entries=entries)


@dataclass
class Commit:
    tree: str
    parents: List[str]
    author: bytes
    committer: bytes
    extra: List[Tuple[bytes, bytes]]
    message: bytes

    def serialize(self) -> bytes:
        out = bytearray()
        out.extend(b"tree " + self.tree.encode("ascii") + b"\n")
        for p in self.parents:
            out.extend(b"parent " + p.encode("ascii") + b"\n")
        out.extend(b"author " + self.author + b"\n")
        out.extend(b"committer " + self.committer + b"\n")
        for k, v in self.extra:
            out.extend(k + b" " + v + b"\n")
        out.extend(b"\n")
        out.extend(self.message)
        return bytes(out)

    @classmethod
    def parse(cls, payload: bytes) -> "Commit":
        lines = payload.split(b"\n")
        sep_idx = -1
        for i, line in enumerate(lines):
            if line == b"":
                sep_idx = i
                break

        if sep_idx != -1:
            header_lines = lines[:sep_idx]
            message = b"\n".join(lines[sep_idx + 1:])
        else:
            header_lines = lines
            message = b""

        tree = ""
        parents = []
        author = b""
        committer = b""
        extra = []

        headers = []
        for line in header_lines:
            if line.startswith(b" "):
                if headers:
                    headers[-1][1] += b"\n" + line
            else:
                parts = line.split(b" ", 1)
                key = parts[0]
                val = parts[1] if len(parts) > 1 else b""
                headers.append([key, val])

        for key, val in headers:
            if key == b"tree":
                tree = val.decode("ascii")
            elif key == b"parent":
                parents.append(val.decode("ascii"))
            elif key == b"author":
                author = val
            elif key == b"committer":
                committer = val
            else:
                extra.append((key, val))

        return cls(
            tree=tree,
            parents=parents,
            author=author,
            committer=committer,
            extra=extra,
            message=message,
        )


@dataclass
class Tag:
    object: str
    type: str
    tag: str
    tagger: Optional[bytes]
    extra: List[Tuple[bytes, bytes]]
    message: bytes

    def serialize(self) -> bytes:
        out = bytearray()
        out.extend(b"object " + self.object.encode("ascii") + b"\n")
        out.extend(b"type " + self.type.encode("ascii") + b"\n")
        out.extend(b"tag " + self.tag.encode("utf-8") + b"\n")
        if self.tagger is not None:
            out.extend(b"tagger " + self.tagger + b"\n")
        for k, v in self.extra:
            out.extend(k + b" " + v + b"\n")
        out.extend(b"\n")
        out.extend(self.message)
        return bytes(out)

    @classmethod
    def parse(cls, payload: bytes) -> "Tag":
        lines = payload.split(b"\n")
        sep_idx = -1
        for i, line in enumerate(lines):
            if line == b"":
                sep_idx = i
                break

        if sep_idx != -1:
            header_lines = lines[:sep_idx]
            message = b"\n".join(lines[sep_idx + 1:])
        else:
            header_lines = lines
            message = b""

        obj_id = ""
        obj_type = ""
        tag_name = ""
        tagger = None
        extra = []

        headers = []
        for line in header_lines:
            if line.startswith(b" "):
                if headers:
                    headers[-1][1] += b"\n" + line
            else:
                parts = line.split(b" ", 1)
                key = parts[0]
                val = parts[1] if len(parts) > 1 else b""
                headers.append([key, val])

        for key, val in headers:
            if key == b"object":
                obj_id = val.decode("ascii")
            elif key == b"type":
                obj_type = val.decode("ascii")
            elif key == b"tag":
                tag_name = val.decode("utf-8", errors="surrogateescape")
            elif key == b"tagger":
                tagger = val
            else:
                extra.append((key, val))

        return cls(
            object=obj_id,
            type=obj_type,
            tag=tag_name,
            tagger=tagger,
            extra=extra,
            message=message,
        )


def parse_object(obj_type: str, payload: bytes) -> Union[bytes, Tree, Commit, Tag]:
    """Parse object payload based on object type."""
    if obj_type == "blob":
        return payload
    elif obj_type == "tree":
        return Tree.parse(payload)
    elif obj_type == "commit":
        return Commit.parse(payload)
    elif obj_type == "tag":
        return Tag.parse(payload)
    else:
        raise ValueError(f"unknown object type: {obj_type}")
