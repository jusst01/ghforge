"""Git packfile processing, delta compression/decompression, and reachability closure."""

import hashlib
import zlib
from typing import List, Tuple, Set, Dict, Optional, Any
from forge.objects import oid_of, Commit, Tag, Tree


def apply_delta(base: bytes, delta: bytes) -> bytes:
    """Apply a git binary delta to a base object payload."""
    pos = 0

    def _read_varint(p: int) -> Tuple[int, int]:
        res = 0
        shift = 0
        while True:
            if p >= len(delta):
                raise ValueError("truncated delta varint")
            b = delta[p]
            p += 1
            res |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break
        return res, p

    src_size, pos = _read_varint(pos)
    if src_size != len(base):
        raise ValueError(f"delta src_size mismatch: expected {len(base)}, got {src_size}")

    tgt_size, pos = _read_varint(pos)
    out = bytearray()
    delta_len = len(delta)

    while pos < delta_len:
        cmd = delta[pos]
        pos += 1
        if cmd & 0x80:
            # COPY opcode
            offset = 0
            if cmd & 0x01:
                if pos >= delta_len:
                    raise ValueError("truncated delta copy offset")
                offset |= delta[pos]
                pos += 1
            if cmd & 0x02:
                if pos >= delta_len:
                    raise ValueError("truncated delta copy offset")
                offset |= delta[pos] << 8
                pos += 1
            if cmd & 0x04:
                if pos >= delta_len:
                    raise ValueError("truncated delta copy offset")
                offset |= delta[pos] << 16
                pos += 1
            if cmd & 0x08:
                if pos >= delta_len:
                    raise ValueError("truncated delta copy offset")
                offset |= delta[pos] << 24
                pos += 1

            size = 0
            if cmd & 0x10:
                if pos >= delta_len:
                    raise ValueError("truncated delta copy size")
                size |= delta[pos]
                pos += 1
            if cmd & 0x20:
                if pos >= delta_len:
                    raise ValueError("truncated delta copy size")
                size |= delta[pos] << 8
                pos += 1
            if cmd & 0x40:
                if pos >= delta_len:
                    raise ValueError("truncated delta copy size")
                size |= delta[pos] << 16
                pos += 1

            if size == 0:
                size = 0x10000

            if offset + size > len(base):
                raise ValueError("delta copy beyond base size")

            out.extend(base[offset : offset + size])
        elif cmd > 0:
            # INSERT opcode
            if pos + cmd > delta_len:
                raise ValueError("delta insert beyond delta size")
            out.extend(delta[pos : pos + cmd])
            pos += cmd
        else:
            raise ValueError("invalid delta opcode 0")

    if len(out) != tgt_size:
        raise ValueError(f"delta target size mismatch: expected {tgt_size}, got {len(out)}")

    return bytes(out)


TYPE_NAMES = {
    1: "commit",
    2: "tree",
    3: "blob",
    4: "tag",
}

TYPE_NUMS = {
    "commit": 1,
    "tree": 2,
    "blob": 3,
    "tag": 4,
}


def read_pack(data: bytes, resolve_external=None) -> List[Tuple[str, str, bytes]]:
    """
    Read a version 2 packfile from bytes, resolving ofs/ref deltas.
    Returns list of (oid, type_str, payload) for every entry.
    """
    if len(data) < 32:
        raise ValueError("packfile too short")

    if data[:4] != b"PACK":
        raise ValueError("invalid packfile signature")

    version = int.from_bytes(data[4:8], "big")
    if version != 2:
        raise ValueError(f"unsupported pack version {version}")

    num_objects = int.from_bytes(data[8:12], "big")

    # Verify SHA-1 trailer
    expected_sha = hashlib.sha1(data[:-20]).digest()
    file_sha = data[-20:]
    if expected_sha != file_sha:
        raise ValueError("pack checksum mismatch")

    pos = 12
    pack_len = len(data) - 20

    raw_entries = []
    offset_to_idx = {}

    for i in range(num_objects):
        if pos >= pack_len:
            raise ValueError("unexpected end of packfile data")

        entry_offset = pos
        offset_to_idx[entry_offset] = i

        b = data[pos]
        pos += 1
        type_num = (b >> 4) & 0x07
        size = b & 0x0F
        shift = 4
        while b & 0x80:
            if pos >= pack_len:
                raise ValueError("truncated entry header")
            b = data[pos]
            pos += 1
            size |= (b & 0x7F) << shift
            shift += 7

        base_offset = None
        base_oid = None

        if type_num == 6:
            # OFS_DELTA
            if pos >= pack_len:
                raise ValueError("truncated ofs-delta header")
            b = data[pos]
            pos += 1
            delta_off = b & 0x7F
            while b & 0x80:
                if pos >= pack_len:
                    raise ValueError("truncated ofs-delta header")
                b = data[pos]
                pos += 1
                delta_off = ((delta_off + 1) << 7) | (b & 0x7F)

            base_offset = entry_offset - delta_off
            if base_offset >= entry_offset or base_offset < 12:
                raise ValueError("invalid ofs-delta offset")
        elif type_num == 7:
            # REF_DELTA
            if pos + 20 > pack_len:
                raise ValueError("truncated ref-delta header")
            base_oid_bytes = data[pos : pos + 20]
            pos += 20
            base_oid = base_oid_bytes.hex()
        elif type_num not in TYPE_NAMES:
            raise ValueError(f"unknown pack object type {type_num}")

        # Decompress zlib stream
        decompressor = zlib.decompressobj()
        try:
            raw_payload = decompressor.decompress(data[pos:pack_len])
        except zlib.error as e:
            raise ValueError("corrupt zlib stream in pack") from e

        consumed = (pack_len - pos) - len(decompressor.unused_data)
        if consumed <= 0:
            raise ValueError("invalid zlib stream length")

        pos += consumed

        if len(raw_payload) != size:
            raise ValueError(f"decompressed size mismatch: expected {size}, got {len(raw_payload)}")

        raw_entries.append({
            "offset": entry_offset,
            "type_num": type_num,
            "raw_payload": raw_payload,
            "base_offset": base_offset,
            "base_oid": base_oid,
        })

    if pos != pack_len:
        raise ValueError("pack data size mismatch: extra bytes before trailer")

    resolved_by_idx: Dict[int, Tuple[str, str, bytes]] = {}
    resolved_by_oid: Dict[str, Tuple[str, str, bytes]] = {}
    oid_to_idx: Dict[str, int] = {}

    def resolve(idx: int, visited: Set[int]) -> Tuple[str, str, bytes]:
        if idx in visited:
            raise ValueError("cyclic delta dependency")
        if idx in resolved_by_idx:
            return resolved_by_idx[idx]

        entry = raw_entries[idx]
        t_num = entry["type_num"]
        raw_payload = entry["raw_payload"]

        if t_num in TYPE_NAMES:
            t_str = TYPE_NAMES[t_num]
            oid = oid_of(t_str, raw_payload)
            res = (oid, t_str, raw_payload)
            resolved_by_idx[idx] = res
            resolved_by_oid[oid] = res
            oid_to_idx[oid] = idx
            return res
        elif t_num == 6:
            # OFS_DELTA
            b_off = entry["base_offset"]
            if b_off not in offset_to_idx:
                raise ValueError(f"invalid ofs-delta offset {b_off}")
            b_idx = offset_to_idx[b_off]
            _, b_type, b_payload = resolve(b_idx, visited | {idx})
            res_payload = apply_delta(b_payload, raw_payload)
            oid = oid_of(b_type, res_payload)
            res = (oid, b_type, res_payload)
            resolved_by_idx[idx] = res
            resolved_by_oid[oid] = res
            oid_to_idx[oid] = idx
            return res
        elif t_num == 7:
            # REF_DELTA
            b_oid = entry["base_oid"]
            if b_oid in resolved_by_oid:
                _, b_type, b_payload = resolved_by_oid[b_oid]
            elif b_oid in oid_to_idx:
                b_idx = oid_to_idx[b_oid]
                _, b_type, b_payload = resolve(b_idx, visited | {idx})
            elif resolve_external is not None:
                ext = resolve_external(b_oid)
                if ext is None:
                    raise ValueError(f"external base object {b_oid} not found")
                b_type, b_payload = ext
            else:
                raise ValueError(f"ref-delta base object {b_oid} not found")

            res_payload = apply_delta(b_payload, raw_payload)
            oid = oid_of(b_type, res_payload)
            res = (oid, b_type, res_payload)
            resolved_by_idx[idx] = res
            resolved_by_oid[oid] = res
            oid_to_idx[oid] = idx
            return res
        else:
            raise ValueError(f"unknown object type {t_num}")

    # Pre-resolve non-deltas so their OIDs are indexed
    for i, entry in enumerate(raw_entries):
        if entry["type_num"] in TYPE_NAMES:
            resolve(i, set())

    # Resolve all entries
    result = []
    for i in range(num_objects):
        res = resolve(i, set())
        result.append(res)

    return result


def build_pack(objects: List[Tuple[str, bytes]]) -> bytes:
    """
    Build a version 2 packfile with no deltas from a list of (type, payload) tuples.
    """
    out = bytearray()
    out.extend(b"PACK")
    out.extend((2).to_bytes(4, "big"))
    out.extend(len(objects).to_bytes(4, "big"))

    for type_str, payload in objects:
        if type_str not in TYPE_NUMS:
            raise ValueError(f"unknown object type {type_str}")
        type_num = TYPE_NUMS[type_str]
        size = len(payload)

        # Header encoding
        header = bytearray()
        first_byte = (type_num << 4) | (size & 0x0F)
        size >>= 4
        if size > 0:
            first_byte |= 0x80
        header.append(first_byte)

        while size > 0:
            b = size & 0x7F
            size >>= 7
            if size > 0:
                b |= 0x80
            header.append(b)

        compressed = zlib.compress(payload, level=6)
        out.extend(header)
        out.extend(compressed)

    sha = hashlib.sha1(out).digest()
    out.extend(sha)
    return bytes(out)


def walk_closure(
    store: Any,
    tips: List[str],
    exclude: Optional[Set[str]] = None,
    depth: Optional[int] = None,
) -> Tuple[Set[str], Set[str]]:
    """
    Compute reachable object OIDs from tips, respecting exclude set and depth limit.
    Returns (included_oids, boundary_commits).
    """
    if exclude is None:
        exclude_set = set()
    else:
        exclude_set = set(exclude)

    def get_obj(oid: str) -> Optional[Tuple[str, bytes]]:
        if hasattr(store, "get_object"):
            return store.get_object(oid)
        if hasattr(store, "objects") and isinstance(store.objects, dict):
            return store.objects.get(oid)
        if hasattr(store, "get"):
            res = store.get(oid)
            if res is not None:
                return res
        try:
            return store[oid]
        except (KeyError, TypeError):
            return None

    # Compute excluded commit closure
    excluded_commits = set()
    exclude_queue = list(exclude_set)
    visited_exclude = set(exclude_set)

    while exclude_queue:
        curr_oid = exclude_queue.pop(0)
        obj = get_obj(curr_oid)
        if not obj:
            continue
        obj_type, payload = obj

        # Peel tags
        while obj_type == "tag":
            tag_obj = Tag.parse(payload)
            target_oid = tag_obj.object
            target = get_obj(target_oid)
            if not target:
                break
            obj_type, payload = target
            curr_oid = target_oid

        if obj_type == "commit":
            excluded_commits.add(curr_oid)
            commit_obj = Commit.parse(payload)
            for parent in commit_obj.parents:
                if parent not in visited_exclude:
                    visited_exclude.add(parent)
                    exclude_queue.append(parent)

    # Peel tip tags and collect tip commits
    commit_tips = []
    other_tips = set()

    for tip_oid in tips:
        obj = get_obj(tip_oid)
        if not obj:
            continue
        obj_type, payload = obj
        curr_oid = tip_oid

        while obj_type == "tag":
            tag_obj = Tag.parse(payload)
            target_oid = tag_obj.object
            target = get_obj(target_oid)
            if not target:
                break
            obj_type, payload = target
            curr_oid = target_oid

        if obj_type == "commit":
            if curr_oid not in excluded_commits:
                commit_tips.append(curr_oid)
        else:
            other_tips.add(tip_oid)
            other_tips.add(curr_oid)

    # BFS commit traversal with generation numbers
    included_commits = set()
    commit_gen = {}
    boundary_commits = set()

    queue = []
    for c_oid in commit_tips:
        if c_oid not in commit_gen:
            commit_gen[c_oid] = 0
            queue.append((c_oid, 0))

    while queue:
        c_oid, gen = queue.pop(0)
        included_commits.add(c_oid)

        obj = get_obj(c_oid)
        if not obj:
            continue
        obj_type, payload = obj
        if obj_type != "commit":
            continue

        commit = Commit.parse(payload)

        if depth is not None and gen == depth - 1:
            # Check if parents were cut by depth boundary
            if commit.parents:
                boundary_commits.add(c_oid)
            continue

        for parent_oid in commit.parents:
            if parent_oid in excluded_commits:
                continue
            if parent_oid not in commit_gen:
                commit_gen[parent_oid] = gen + 1
                queue.append((parent_oid, gen + 1))

    # Collect tree and blob OIDs from included commits and other tips
    included_oids = set(included_commits) | other_tips
    visited_trees = set()

    def walk_tree(tree_oid: str):
        if tree_oid in visited_trees:
            return
        visited_trees.add(tree_oid)
        included_oids.add(tree_oid)

        t_obj = get_obj(tree_oid)
        if not t_obj:
            return
        t_type, t_payload = t_obj
        if t_type != "tree":
            return
        tree = Tree.parse(t_payload)
        for entry in tree.entries:
            included_oids.add(entry.oid)
            if (entry.mode & 0o170000) == 0o040000:
                walk_tree(entry.oid)

    for c_oid in included_commits:
        obj = get_obj(c_oid)
        if not obj:
            continue
        obj_type, payload = obj
        if obj_type == "commit":
            commit = Commit.parse(payload)
            walk_tree(commit.tree)

    for t_oid in list(other_tips):
        obj = get_obj(t_oid)
        if not obj:
            continue
        obj_type, payload = obj
        if obj_type == "tree":
            walk_tree(t_oid)

    # Collect tags in store pointing to included objects
    all_oids = []
    if hasattr(store, "keys"):
        all_oids = list(store.keys())
    elif hasattr(store, "objects") and hasattr(store.objects, "keys"):
        all_oids = list(store.objects.keys())

    for oid in all_oids:
        if oid in included_oids:
            continue
        obj = get_obj(oid)
        if not obj or obj[0] != "tag":
            continue

        curr_oid = oid
        curr_obj = obj
        tag_chain = []
        while curr_obj and curr_obj[0] == "tag":
            tag_chain.append(curr_oid)
            tag = Tag.parse(curr_obj[1])
            curr_oid = tag.object
            curr_obj = get_obj(curr_oid)

        if curr_oid in included_oids:
            included_oids.update(tag_chain)

    return included_oids, boundary_commits
