"""In-memory object store, repositories, issues, pulls, and logical clock for the forge."""

import base64
import datetime
import hashlib
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

# B6 identity constants
U64_MASK = 0xFFFFFFFFFFFFFFFF
SCENARIO_SEED = 0x6a09e667f3bcc909

def fnv1a64(text: str) -> int:
    hash_val = 0xcbf29ce484222325
    for ch in text:
        hash_val ^= ord(ch)
        hash_val = (hash_val * 0x100000001b3) & U64_MASK
    return hash_val

def mix64(v: int) -> int:
    z = (v + 0x9e3779b97f4a7c15) & U64_MASK
    z = ((z ^ (z >> 30)) * 0xbf58476d1ce4e5b9) & U64_MASK
    z = ((z ^ (z >> 27)) * 0x94d049bb133111eb) & U64_MASK
    return (z ^ (z >> 31)) & U64_MASK

def scenarioSeedFor(label: str) -> int:
    return mix64(SCENARIO_SEED ^ fnv1a64(label))

class Rng:
    def __init__(self, seed: int):
        self.state = seed & U64_MASK
        if self.state == 0:
            self.state = SCENARIO_SEED

    def nextU64(self) -> int:
        x = self.state
        x = (x ^ (x >> 12)) & U64_MASK
        x = (x ^ (x << 25)) & U64_MASK
        x = (x ^ (x >> 27)) & U64_MASK
        self.state = x
        return (x * 0x2545f4914f6cdd1d) & U64_MASK

    def nextU32(self) -> int:
        return self.nextU64() >> 32

    def nextInt(self, n: int) -> int:
        if n == 1:
            return 0
        limit = 0x100000000 - (0x100000000 % n)
        while True:
            val = self.nextU32()
            if val < limit:
                return val % n

    def pick(self, list_items: list):
        return list_items[self.nextInt(len(list_items))]

FIRST_NAMES = "ada bram cora dmitri elena felix greta hugo iris jonas kira lars mira nico olga pavel quinn rosa sven tess ulla vito wren yara zane".split()
LAST_NAMES = "anders bauer costa dahl eriksen fischer garcia holm iversen jansen keller lind moreau novak olsen peters quist rossi sato turi ulrich vega weber xu yilmaz zimmer".split()

def generateUsers():
    rng = Rng(scenarioSeedFor("users"))
    users = []
    used_logins = set()
    for i in range(12):
        first = rng.pick(FIRST_NAMES)
        last = rng.pick(LAST_NAMES)
        base = "".join([c for c in (first + last) if c.isalnum() or c == '-'])
        login = base
        count = 2
        while login in used_logins:
            login = f"{base}-{count}"
            count += 1
        used_logins.add(login)
        users.append({
            "id": 1000 + i,
            "login": login,
            "name": f"{first} {last}",
            "email": f"{login}@example.invalid"
        })
    return users

try:
    from forge.objects import oid_of
except ImportError:
    try:
        from objects import oid_of
    except ImportError:
        def oid_of(obj_type: str, payload: bytes) -> str:
            header = f"{obj_type} {len(payload)}\x00".encode("ascii")
            return hashlib.sha1(header + payload).hexdigest()


def gid(kind: str, db_id: int) -> str:
    """Return URL-safe base64 encoding of '<kind>:<db_id>'."""
    raw = f"{kind}:{db_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def parse_gid(s: str) -> Tuple[str, int]:
    """Parse global ID string back to (kind, db_id). Tolerant of bad input."""
    try:
        if not isinstance(s, str) or not s:
            return ("", 0)
        try:
            padding = -len(s) % 4
            s_padded = s + ("=" * padding)
            decoded = base64.urlsafe_b64decode(s_padded.encode("ascii")).decode("utf-8")
            if ":" in decoded:
                parts = decoded.split(":", 1)
                return (parts[0], int(parts[1]))
        except Exception:
            pass
        if ":" in s:
            parts = s.split(":", 1)
            return (parts[0], int(parts[1]))
        return ("", 0)
    except Exception:
        return ("", 0)


@dataclass
class Label:
    name: str
    color: str = "ededed"
    description: str = ""


@dataclass
class Comment:
    id: int
    body: str
    author: str = ""
    created_at: str = ""
    updated_at: str = ""
    issue_number: int = 0


@dataclass
class Issue:
    number: int
    title: str
    body: str = ""
    state: str = "open"
    labels: List[str] = field(default_factory=list)
    author: str = ""
    created_at: str = ""
    updated_at: str = ""
    db_id: int = 0
    comments_count: int = 0


@dataclass
class Pull:
    number: int
    title: str
    body: str = ""
    state: str = "open"
    head_ref: str = ""
    base_ref: str = ""
    head_sha: str = ""
    base_sha: str = ""
    merged: bool = False
    merge_commit_sha: Optional[str] = None
    merged_at: Optional[str] = None
    labels: List[str] = field(default_factory=list)
    author: str = ""
    created_at: str = ""
    updated_at: str = ""
    db_id: int = 0
    comments_count: int = 0


@dataclass
class Repo:
    name: str
    description: str = ""
    private: bool = False
    default_branch: str = "main"
    refs: Dict[str, str] = field(default_factory=dict)
    next_number: int = 1
    issues: Dict[int, Issue] = field(default_factory=dict)
    pulls: Dict[int, Pull] = field(default_factory=dict)
    labels: Dict[str, Label] = field(default_factory=dict)
    comments: Dict[int, List[Comment]] = field(default_factory=dict)
    db_id: int = 0
    pack_cache: Dict[Any, bytes] = field(default_factory=dict)
    cached_json: Dict[str, Any] = field(default_factory=dict)

    def clear_pack_cache(self) -> None:
        self.pack_cache.clear()

    def get_ref(self, refname: str) -> Optional[str]:
        if refname == "HEAD":
            return self.refs.get(f"refs/heads/{self.default_branch}")
        return self.refs.get(refname)

    def set_ref(self, refname: str, oid: str) -> None:
        self.refs[refname] = oid

    def delete_ref(self, refname: str) -> bool:
        if refname in self.refs:
            del self.refs[refname]
            return True
        return False

    def get_issue(self, number: int) -> Optional[Union[Issue, Pull]]:
        if number in self.issues:
            return self.issues[number]
        return self.pulls.get(number)

    def get_pull(self, number: int) -> Optional[Pull]:
        return self.pulls.get(number)

    def get_comments_count(self, number: int) -> int:
        return len(self.comments.get(number, []))


class Store:
    def user_by_login(self, login: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            for user in self.users:
                if user["login"] == login:
                    return user
        return None

    def __init__(self, trial_now: Optional[str] = None):
        self.lock = threading.RLock()
        self.objects: Dict[str, Tuple[str, bytes]] = {}
        self.repos: Dict[str, Repo] = {}
        self.users = generateUsers()
        self.viewer = self.users[0]
        self.teammate = self.users[1]
        self.stranger = self.users[2]
        self.owner_login: str = self.users[0]["login"]
        self.pack_cache: Dict[Tuple[Any, ...], bytes] = {}

        self.next_repo_db_id: int = 1
        self.next_issue_db_id: int = 1
        self.next_pull_db_id: int = 1
        self.next_comment_db_id: int = 1

        raw_now = trial_now if trial_now is not None else os.environ.get("MAKEFUN_TRIAL_NOW")
        self._base_epoch: int = self._parse_trial_now(raw_now)
        self._clock_counter: int = 0

    @staticmethod
    def _parse_trial_now(val: Optional[str]) -> int:
        if not val:
            return 1760000000
        val = str(val).strip()
        try:
            return int(float(val))
        except ValueError:
            pass
        try:
            if val.endswith("Z"):
                val = val[:-1] + "+00:00"
            dt = datetime.datetime.fromisoformat(val)
            return int(dt.timestamp())
        except Exception:
            return 1760000000

    def now_epoch(self) -> int:
        with self.lock:
            epoch = self._base_epoch + self._clock_counter
            self._clock_counter += 1
            return epoch

    def now_iso(self, epoch: Optional[int] = None) -> str:
        if epoch is None:
            epoch = self.now_epoch()
        dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def put(self, obj_type: str, payload: bytes) -> str:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        oid = oid_of(obj_type, payload)
        with self.lock:
            self.objects[oid] = (obj_type, payload)
        return oid

    def get(self, oid: str) -> Tuple[str, bytes]:
        with self.lock:
            if oid not in self.objects:
                raise KeyError(oid)
            return self.objects[oid]

    def has(self, oid: str) -> bool:
        with self.lock:
            return oid in self.objects

    def bind_owner(self, login: str) -> str:
        with self.lock:
            # owner_login is pre-bound, and bind_owner must never override an existing binding
            return self.owner_login

    def get_owner_login(self) -> str:
        with self.lock:
            return self.owner_login

    def create_repo(
        self,
        name: str,
        description: str = "",
        private: bool = False,
        default_branch: str = "main",
    ) -> Repo:
        with self.lock:
            if name in self.repos:
                return self.repos[name]
            db_id = self.next_repo_db_id
            self.next_repo_db_id += 1
            repo = Repo(
                name=name,
                description=description,
                private=private,
                default_branch=default_branch,
                db_id=db_id,
            )
            self.repos[name] = repo
            return repo

    def get_repo(self, name: str) -> Optional[Repo]:
        with self.lock:
            return self.repos.get(name)

    def clear_pack_cache(self, repo: Union[str, Repo]) -> None:
        with self.lock:
            repo_name = repo.name if isinstance(repo, Repo) else str(repo)
            r = self.repos.get(repo_name)
            if r is not None:
                r.clear_pack_cache()
            to_del = [
                k
                for k in self.pack_cache
                if isinstance(k, tuple) and len(k) > 0 and k[0] == repo_name
            ]
            for k in to_del:
                del self.pack_cache[k]

    def get_pack_cache(self, key: tuple) -> Optional[bytes]:
        with self.lock:
            return self.pack_cache.get(key)

    def set_pack_cache(self, key: tuple, data: bytes) -> None:
        with self.lock:
            if len(self.pack_cache) >= 64:
                self.pack_cache.clear()
            self.pack_cache[key] = data
