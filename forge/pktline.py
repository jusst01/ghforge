"""Git pkt-line framing and sideband utilities."""

class _Sentinel:
    def __init__(self, name: str, val: bytes):
        self.name = name
        self.val = val

    def __repr__(self) -> str:
        return f"<{self.name}>"

    def __eq__(self, other) -> bool:
        return other is self or other == self.val or other == self.name

FLUSH = _Sentinel("FLUSH", b"0000")
DELIM = _Sentinel("DELIM", b"0001")
RESP_END = _Sentinel("RESP_END", b"0002")

def pkt(payload: bytes) -> bytes:
    """Format payload as a git pkt-line."""
    length = len(payload) + 4
    if length > 65535:
        raise ValueError(f"pkt payload too large: {length}")
    return f"{length:04x}".encode("ascii") + payload

def flush() -> bytes:
    """Return a flush packet (0000)."""
    return b"0000"

def delim() -> bytes:
    """Return a delimiter packet (0001)."""
    return b"0001"

def resp_end() -> bytes:
    """Return a response-end packet (0002)."""
    return b"0002"

class PktReader:
    """Reader for git pkt-line framed data streams."""
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read_line(self):
        """Read the next pkt-line payload or sentinel."""
        if self.pos >= len(self.data):
            return None
        if self.pos + 4 > len(self.data):
            raise ValueError("incomplete packet header")
        header_bytes = self.data[self.pos:self.pos + 4]
        try:
            length = int(header_bytes.decode("ascii"), 16)
        except Exception as e:
            raise ValueError(f"malformed pkt-line length: {header_bytes!r}") from e

        self.pos += 4
        if length == 0:
            return FLUSH
        if length == 1:
            return DELIM
        if length == 2:
            return RESP_END
        if length < 4:
            raise ValueError(f"invalid pkt-line length {length}")

        payload_len = length - 4
        if self.pos + payload_len > len(self.data):
            raise ValueError("packet length exceeds remaining data")
        payload = self.data[self.pos:self.pos + payload_len]
        self.pos += payload_len
        return payload

    def rest(self) -> bytes:
        """Return all unconsumed bytes."""
        return self.data[self.pos:]

def sideband(band: int, payload: bytes) -> bytes:
    """Wrap payload in sideband pkt-lines."""
    max_chunk = 65515
    out = bytearray()
    if not payload:
        out.extend(pkt(bytes([band])))
    else:
        for i in range(0, len(payload), max_chunk):
            chunk = payload[i:i + max_chunk]
            line = bytes([band]) + chunk
            out.extend(pkt(line))
    return bytes(out)
