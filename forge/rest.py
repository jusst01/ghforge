"""REST v3 surface implementation for the forge."""

import base64
import datetime
import json
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from forge.objects import Tree, TreeEntry, Commit, Blob, parse_object, format_ident, oid_of
    from forge.store import Store, Repo, Issue, Pull, Comment, Label, gid, parse_gid
except ImportError:
    from objects import Tree, TreeEntry, Commit, Blob, parse_object, format_ident, oid_of
    from store import Store, Repo, Issue, Pull, Comment, Label, gid, parse_gid


def read_tree_files(store: Any, tree_oid: Optional[str]) -> Dict[str, Tuple[int, str]]:
    """Recursively read a tree object and return flat mapping of path -> (mode, blob_oid)."""
    if not tree_oid or not store.has(tree_oid):
        return {}
    try:
        obj_type, payload = store.get(tree_oid)
        if obj_type != "tree":
            return {}
        tree = Tree.parse(payload)
    except Exception:
        return {}

    result = {}
    for entry in tree.entries:
        if entry.is_dir():
            sub_files = read_tree_files(store, entry.oid)
            for sub_path, (mode, oid) in sub_files.items():
                result[f"{entry.name}/{sub_path}"] = (mode, oid)
        else:
            result[entry.name] = (entry.mode, entry.oid)
    return result


def apply_path_change(store: Any, base_tree_oid: Optional[str], path: str, new_mode_and_oid: Optional[Tuple[int, str]]) -> Optional[str]:
    """Build a new tree with one path replaced or removed, and return the new tree OID."""
    files = read_tree_files(store, base_tree_oid)
    if path.startswith("/"):
        path = path[1:]

    if new_mode_and_oid is None:
        files.pop(path, None)
    else:
        files[path] = new_mode_and_oid

    if not files:
        return store.put("tree", b"")

    def build_tree_recursive(current_files: Dict[str, Tuple[int, str]]) -> str:
        dirs: Dict[str, Dict[str, Tuple[int, str]]] = {}
        direct_files: List[TreeEntry] = []

        for p, (mode, oid) in current_files.items():
            if "/" in p:
                parts = p.split("/", 1)
                d = parts[0]
                rest = parts[1]
                if d not in dirs:
                    dirs[d] = {}
                dirs[d][rest] = (mode, oid)
            else:
                direct_files.append(TreeEntry(mode=mode, name=p, oid=oid))

        entries: List[TreeEntry] = list(direct_files)
        for d, sub_files in dirs.items():
            sub_tree_oid = build_tree_recursive(sub_files)
            entries.append(TreeEntry(mode=0o040000, name=d, oid=sub_tree_oid))

        tree_obj = Tree(entries=entries)
        payload = tree_obj.serialize()
        return store.put("tree", payload)

    return build_tree_recursive(files)


def commit_on_branch(
    store: Any,
    repo: Any,
    branch_name: str,
    parent_oids: List[str],
    tree_oid: str,
    message: str,
    author_name: Optional[str] = None,
    author_email: Optional[str] = None,
) -> str:
    """Create a commit object on top of branch and update ref."""
    if author_name is None:
        author_name = store.viewer["name"]
    if author_email is None:
        author_email = store.viewer["email"]

    epoch = store.now_epoch()
    ident = format_ident(author_name, author_email, epoch)

    msg_bytes = message.encode("utf-8") if isinstance(message, str) else message
    if not msg_bytes.endswith(b"\n"):
        msg_bytes += b"\n"

    commit = Commit(
        tree=tree_oid,
        parents=parent_oids,
        author=ident,
        committer=ident,
        extra=[],
        message=msg_bytes,
    )
    payload = commit.serialize()
    commit_oid = store.put("commit", payload)

    repo.set_ref(f"refs/heads/{branch_name}", commit_oid)
    return commit_oid


def make_user_obj(store: Any) -> Dict[str, Any]:
    login = store.get_owner_login()
    return {
        "login": login,
        "id": 1000,
        "node_id": gid("User", 1000),
        "type": "User",
        "site_admin": False
    }


def make_repo_obj(store: Any, repo: Any, base_url: str = "") -> Dict[str, Any]:
    login = store.get_owner_login()
    full_name = f"{login}/{repo.name}"
    open_issues = len([i for i in repo.issues.values() if i.state == "open"]) + len([p for p in repo.pulls.values() if p.state == "open"])
    html_url = f"{base_url}/{full_name}" if base_url else f"https://github.com/{full_name}"
    url = f"{base_url}/api/v3/repos/{full_name}" if base_url else f"https://api.github.com/repos/{full_name}"
    return {
        "id": repo.db_id,
        "node_id": gid("Repository", repo.db_id),
        "name": repo.name,
        "full_name": full_name,
        "owner": make_user_obj(store),
        "private": repo.private,
        "description": repo.description,
        "default_branch": repo.default_branch,
        "html_url": html_url,
        "url": url,
        "created_at": store.now_iso(),
        "updated_at": store.now_iso(),
        "size": 0,
        "stargazers_count": 0,
        "watchers_count": 0,
        "forks_count": 0,
        "open_issues_count": open_issues,
        "has_issues": True,
        "archived": False,
        "disabled": False,
        "visibility": "private" if repo.private else "public",
        "license": None,
        "topics": [],
        "language": None
    }


def make_label_obj(store: Any, repo: Any, label: Any, base_url: str = "") -> Dict[str, Any]:
    login = store.get_owner_login()
    url = f"{base_url}/api/v3/repos/{login}/{repo.name}/labels/{label.name}"
    lbl_id = abs(hash(label.name)) % 1000000000
    return {
        "id": lbl_id,
        "node_id": gid("Label", lbl_id),
        "url": url,
        "name": label.name,
        "color": label.color,
        "description": label.description,
        "default": False
    }


def make_issue_obj(store: Any, repo: Any, issue: Any, base_url: str = "") -> Dict[str, Any]:
    login = store.get_owner_login()
    repo_name = repo.name
    url = f"{base_url}/api/v3/repos/{login}/{repo_name}/issues/{issue.number}"
    html_url = f"{base_url}/{login}/{repo_name}/issues/{issue.number}"
    repo_url = f"{base_url}/api/v3/repos/{login}/{repo_name}"
    label_objs = [make_label_obj(store, repo, repo.labels[ln]) for ln in issue.labels if ln in repo.labels]
    comments_count = repo.get_comments_count(issue.number)
    is_pull = isinstance(issue, Pull)
    obj = {
        "id": issue.db_id,
        "node_id": gid("Issue" if not is_pull else "PullRequest", issue.db_id),
        "url": url,
        "repository_url": repo_url,
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "state": issue.state,
        "labels": label_objs,
        "user": make_user_obj(store),
        "assignees": [],
        "comments": comments_count,
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
        "closed_at": getattr(issue, 'closed_at', None) or (issue.created_at if issue.state == 'closed' else None),
        "html_url": html_url
    }
    if is_pull:
        pull_url = f"{base_url}/api/v3/repos/{login}/{repo_name}/pulls/{issue.number}"
        pull_html_url = f"{base_url}/{login}/{repo_name}/pulls/{issue.number}"
        obj["pull_request"] = {
            "url": pull_url,
            "html_url": pull_html_url,
            "diff_url": f"{pull_url}.diff",
            "patch_url": f"{pull_url}.patch"
        }
    return obj


def make_comment_obj(store: Any, repo: Any, issue_number: int, comment: Any, base_url: str = "") -> Dict[str, Any]:
    login = store.get_owner_login()
    repo_name = repo.name
    url = f"{base_url}/api/v3/repos/{login}/{repo_name}/issues/comments/{comment.id}"
    issue_url = f"{base_url}/api/v3/repos/{login}/{repo_name}/issues/{issue_number}"
    html_url = f"{base_url}/{login}/{repo_name}/issues/{issue_number}#issuecomment-{comment.id}"
    return {
        "id": comment.id,
        "node_id": gid("IssueComment", comment.id),
        "url": url,
        "body": comment.body,
        "user": make_user_obj(store),
        "created_at": comment.created_at,
        "updated_at": comment.updated_at,
        "html_url": html_url,
        "issue_url": issue_url
    }


def parse_body(body: Any) -> Any:
    if body is None or body == b"" or body == "":
        return {}
    if isinstance(body, (dict, list)):
        return body
    if isinstance(body, bytes):
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            raise ValueError("malformed JSON")
    if isinstance(body, str):
        try:
            return json.loads(body)
        except Exception:
            raise ValueError("malformed JSON")
    return {}


def get_tree_at_path(store: Any, tree_oid: str, path_parts: List[str]) -> Tuple[Optional[str], Optional[str]]:
    if not path_parts or path_parts == [""]:
        return "tree", tree_oid

    current_oid = tree_oid
    for part in path_parts:
        if not current_oid or not store.has(current_oid):
            return None, None
        try:
            obj_type, payload = store.get(current_oid)
        except Exception:
            return None, None
        if obj_type != "tree":
            return None, None
        try:
            tree = Tree.parse(payload)
        except Exception:
            return None, None
        found = None
        for entry in tree.entries:
            if entry.name == part:
                found = entry
                break
        if not found:
            return None, None
        current_oid = found.oid
        if part == path_parts[-1]:
            if found.is_dir():
                return "tree", current_oid
            else:
                return "blob", current_oid
    return None, None


def handle_rest(store: Any, method: str, seg: Any, query: Any, body: Any, base_url: str = "") -> Optional[Tuple[int, Any]]:
    method = method.upper()

    if isinstance(seg, str):
        seg = [s for s in seg.strip("/").split("/") if s]
    elif isinstance(seg, (list, tuple)):
        seg = [str(s) for s in seg if s]
    else:
        seg = []

    if seg and seg[0] == "api":
        seg = seg[1:]
    if seg and seg[0] == "v3":
        seg = seg[1:]

    q = {}
    if isinstance(query, str):
        from urllib.parse import parse_qs
        parsed = parse_qs(query)
        for k, v in parsed.items():
            q[k] = v[0] if len(v) == 1 else v
    elif isinstance(query, dict):
        for k, v in query.items():
            if isinstance(v, list) and v:
                q[k] = v[0]
            else:
                q[k] = v

    try:
        req_body = parse_body(body)
    except ValueError:
        return 400, {"message": "Malformed JSON"}

    # Routes handling
    if not seg:
        if method == "GET":
            return 200, {}
        return 404, {"message": "Not Found"}

    if seg == ["meta"]:
        if method == "GET":
            return 200, {"installed_version": "1.0.0"}
        return 404, {"message": "Not Found"}

    if seg == ["user"]:
        if method == "GET":
            return 200, make_user_obj(store)
        return 404, {"message": "Not Found"}

    if seg == ["user", "repos"]:
        if method == "GET":
            repos = sorted(store.repos.values(), key=lambda r: r.db_id)
            per_page = int(q.get("per_page", 30))
            page = int(q.get("page", 1))
            start = (page - 1) * per_page
            end = start + per_page
            repos = repos[start:end]
            return 200, [make_repo_obj(store, r, base_url) for r in repos]
        elif method == "POST":
            name = req_body.get("name")
            if not name or not isinstance(name, str) or not name.strip():
                return 422, {"message": "Invalid name"}
            if name in store.repos:
                return 422, {"message": "Repository already exists"}
            desc = req_body.get("description", "")
            private = bool(req_body.get("private", False))
            if store.owner_login is None:
                store.bind_owner("owner")
            repo = store.create_repo(name=name, description=desc, private=private)
            return 201, make_repo_obj(store, repo, base_url)
        return 404, {"message": "Not Found"}

    if len(seg) >= 3 and seg[0] == "repos":
        o = seg[1]
        r = seg[2]
        bound = store.get_owner_login()
        if store.owner_login is None:
            store.bind_owner(o)
        elif o != store.owner_login:
            return 404, {"message": "Not Found"}

        repo = store.get_repo(r)
        if not repo:
            return 404, {"message": "Not Found"}

        sub = seg[3:]

        # Pulls routes return None
        if sub and sub[0] == "pulls":
            return None

        if not sub:
            if method == "GET":
                return 200, make_repo_obj(store, repo, base_url)
            elif method == "PATCH":
                if "description" in req_body:
                    repo.description = req_body["description"]
                if "private" in req_body:
                    repo.private = bool(req_body["private"])
                if "default_branch" in req_body:
                    repo.default_branch = req_body["default_branch"]
                if "name" in req_body:
                    new_name = req_body["name"]
                    if new_name != repo.name:
                        if new_name in store.repos:
                            return 422, {"message": "Repository already exists"}
                        store.repos.pop(repo.name, None)
                        repo.name = new_name
                        store.repos[new_name] = repo
                return 200, make_repo_obj(store, repo, base_url)
            elif method == "DELETE":
                store.repos.pop(repo.name, None)
                store.clear_pack_cache(repo)
                return 204, None
            return 404, {"message": "Not Found"}

        if sub == ["branches"]:
            if method == "GET":
                branches = []
                login = store.get_owner_login()
                for refname, sha in sorted(repo.refs.items(), key=lambda x: x[0]):
                    if refname.startswith("refs/heads/"):
                        bname = refname[len("refs/heads/"):]
                        commit_url = f"{base_url}/api/v3/repos/{login}/{repo.name}/commits/{sha}"
                        branches.append({
                            "name": bname,
                            "commit": {"sha": sha, "url": commit_url},
                            "protected": False
                        })
                return 200, branches
            return 404, {"message": "Not Found"}

        if len(sub) == 2 and sub[0] == "branches":
            bname = sub[1]
            if method == "GET":
                sha = repo.get_ref(f"refs/heads/{bname}")
                if not sha:
                    return 404, {"message": "Branch not found"}
                login = store.get_owner_login()
                commit_url = f"{base_url}/api/v3/repos/{login}/{repo.name}/commits/{sha}"
                return 200, {
                    "name": bname,
                    "commit": {"sha": sha, "url": commit_url},
                    "protected": False
                }
            return 404, {"message": "Not Found"}

        if sub == ["git", "refs"]:
            if method == "POST":
                ref = req_body.get("ref", "")
                sha = req_body.get("sha", "")
                if not store.has(sha):
                    return 422, {"message": "Sha not found"}
                if ref in repo.refs:
                    return 422, {"message": "Ref already exists"}
                repo.set_ref(ref, sha)
                login = store.get_owner_login()
                ref_id = abs(hash(ref)) % 1000000000
                return 201, {
                    "ref": ref,
                    "node_id": gid("Ref", ref_id),
                    "url": f"{base_url}/api/v3/repos/{login}/{repo.name}/git/refs/{ref}",
                    "object": {"sha": sha, "type": "commit", "url": f"{base_url}/api/v3/repos/{login}/{repo.name}/git/commits/{sha}"}
                }
            return 404, {"message": "Not Found"}

        if sub == ["commits"]:
            if method == "GET":
                sha_or_ref = q.get("sha") or repo.default_branch
                tip_oid = repo.get_ref(f"refs/heads/{sha_or_ref}") or repo.get_ref(sha_or_ref) or sha_or_ref
                if not tip_oid or not store.has(tip_oid):
                    default_tip = repo.get_ref(f"refs/heads/{repo.default_branch}")
                    if not default_tip:
                        return 409, {"message": "Git Repository is empty"}
                    return 422, {"message": "Reference not found"}

                commits_list = []
                visited = set()
                queue = [tip_oid]
                per_page = int(q.get("per_page", 30))
                login = store.get_owner_login()

                while queue and len(commits_list) < per_page:
                    curr_oid = queue.pop(0)
                    if curr_oid in visited:
                        continue
                    visited.add(curr_oid)
                    if not store.has(curr_oid):
                        continue
                    try:
                        obj_type, payload = store.get(curr_oid)
                        if obj_type != "commit":
                            continue
                        c_obj = Commit.parse(payload)
                    except Exception:
                        continue

                    def parse_ident_bytes(b: bytes):
                        try:
                            text = b.decode("utf-8")
                            if "<" in text and ">" in text:
                                parts = text.split("<", 1)
                                name = parts[0].strip()
                                rest = parts[1]
                                email_parts = rest.split(">", 1)
                                email = email_parts[0].strip()
                                date_part = email_parts[1].strip().split()
                                epoch = int(date_part[0]) if date_part else 1760000000
                                dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
                                return name, email, dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                        except Exception:
                            pass
                        return "Author", "author@localhost", "2025-01-01T00:00:00Z"

                    auth_name, auth_email, auth_date = parse_ident_bytes(c_obj.author)
                    comm_name, comm_email, comm_date = parse_ident_bytes(c_obj.committer)
                    msg_str = c_obj.message.decode("utf-8", errors="replace").rstrip("\n")

                    commits_list.append({
                        "sha": curr_oid,
                        "node_id": gid("Commit", abs(hash(curr_oid)) % 1000000000),
                        "commit": {
                            "author": {"name": auth_name, "email": auth_email, "date": auth_date},
                            "committer": {"name": comm_name, "email": comm_email, "date": comm_date},
                            "message": msg_str,
                            "tree": {"sha": c_obj.tree, "url": f"{base_url}/api/v3/repos/{login}/{repo.name}/git/trees/{c_obj.tree}"},
                            "url": f"{base_url}/api/v3/repos/{login}/{repo.name}/git/commits/{curr_oid}"
                        },
                        "author": make_user_obj(store),
                        "committer": make_user_obj(store),
                        "parents": [{"sha": p, "url": f"{base_url}/api/v3/repos/{login}/{repo.name}/commits/{p}"} for p in c_obj.parents]
                    })

                    for p in c_obj.parents:
                        if p not in visited:
                            queue.append(p)

                return 200, commits_list
            return 404, {"message": "Not Found"}

        if sub and sub[0] == "contents":
            path_parts = sub[1:]
            file_path = "/not/empty" if not path_parts else "/".join(path_parts)
            file_name = path_parts[-1] if path_parts else ""

            ref = q.get("ref") or q.get("branch") or repo.default_branch
            tip_oid = repo.get_ref(f"refs/heads/{ref}") or repo.get_ref(ref) or ref

            tree_oid = None
            if tip_oid and store.has(tip_oid):
                try:
                    obj_type, payload = store.get(tip_oid)
                    if obj_type == "commit":
                        tree_oid = Commit.parse(payload).tree
                    elif obj_type == "tree":
                        tree_oid = tip_oid
                except Exception:
                    pass

            if method == "GET":
                if not tree_oid:
                    return 404, {"message": "Not Found"}
                t_type, t_oid = get_tree_at_path(store, tree_oid, path_parts)
                if not t_type:
                    return 404, {"message": "Not Found"}
                login = store.get_owner_login()
                if t_type == "blob":
                    _, blob_payload = store.get(t_oid)
                    encoded = base64.b64encode(blob_payload).decode("ascii")
                    url = f"{base_url}/api/v3/repos/{login}/{repo.name}/contents/{file_path}"
                    return 200, {
                        "name": file_name,
                        "path": file_path,
                        "sha": t_oid,
                        "size": len(blob_payload),
                        "type": "file",
                        "encoding": "base64",
                        "content": encoded,
                        "url": url,
                        "html_url": f"{base_url}/{login}/{repo.name}/blob/{ref}/{file_path}",
                        "download_url": f"{base_url}/raw/{login}/{repo.name}/{ref}/{file_path}"
                    }
                else:
                    _, tree_payload = store.get(t_oid)
                    tree = Tree.parse(tree_payload)
                    items = []
                    dir_path = "/".join(path_parts) if path_parts else ""
                    for entry in tree.entries:
                        sub_p = f"{dir_path}/{entry.name}" if dir_path else entry.name
                        item_type = "dir" if entry.is_dir() else "file"
                        item_url = f"{base_url}/api/v3/repos/{login}/{repo.name}/contents/{sub_p}"
                        sub_size = 0
                        if item_type == "file" and store.has(entry.oid):
                            try:
                                sub_size = len(store.get(entry.oid)[1])
                            except Exception:
                                pass
                        items.append({
                            "name": entry.name,
                            "path": sub_p,
                            "sha": entry.oid,
                            "size": sub_size,
                            "type": item_type,
                            "url": item_url,
                            "html_url": f"{base_url}/{login}/{repo.name}/tree/{ref}/{sub_p}" if item_type == "dir" else f"{base_url}/{login}/{repo.name}/blob/{ref}/{sub_p}",
                            "download_url": None if item_type == "dir" else f"{base_url}/raw/{login}/{repo.name}/{ref}/{sub_p}"
                        })
                    return 200, sorted(items, key=lambda x: x["name"])

            elif method == "PUT":
                message = req_body.get("message", "Update file")
                content_b64 = req_body.get("content", "")
                sha_param = req_body.get("sha")
                branch = req_body.get("branch") or repo.default_branch

                content_bytes = base64.b64decode(content_b64) if content_b64 else b""
                blob_oid = store.put("blob", content_bytes)

                b_tip = repo.get_ref(f"refs/heads/{branch}")
                base_t_oid = None
                file_existed = False
                curr_blob_oid = None

                if b_tip and store.has(b_tip):
                    try:
                        _, c_pay = store.get(b_tip)
                        base_t_oid = Commit.parse(c_pay).tree
                        t_t, t_o = get_tree_at_path(store, base_t_oid, path_parts)
                        if t_t == "blob":
                            file_existed = True
                            curr_blob_oid = t_o
                    except Exception:
                        pass

                if sha_param and file_existed and sha_param != curr_blob_oid:
                    return 409, {"message": "sha mismatch"}

                new_t_oid = apply_path_change(store, base_t_oid, file_path, (0o100644, blob_oid))
                parents = [b_tip] if b_tip else []
                commit_oid = commit_on_branch(store, repo, branch, parents, new_t_oid, message)

                status = 200 if file_existed else 201
                return status, {
                    "content": {
                        "name": file_name,
                        "path": file_path,
                        "sha": blob_oid,
                        "size": len(content_bytes),
                        "type": "file",
                        "encoding": "base64"
                    },
                    "commit": {
                        "sha": commit_oid
                    }
                }

            elif method == "DELETE":
                message = req_body.get("message", "Delete file")
                sha_param = req_body.get("sha")
                branch = req_body.get("branch") or repo.default_branch

                b_tip = repo.get_ref(f"refs/heads/{branch}")
                if not b_tip or not store.has(b_tip):
                    return 404, {"message": "Not Found"}

                try:
                    _, c_pay = store.get(b_tip)
                    base_t_oid = Commit.parse(c_pay).tree
                    t_t, t_o = get_tree_at_path(store, base_t_oid, path_parts)
                    if t_t != "blob":
                        return 404, {"message": "Not Found"}
                    if sha_param and sha_param != t_o:
                        return 409, {"message": "sha mismatch"}
                except Exception:
                    return 404, {"message": "Not Found"}

                new_t_oid = apply_path_change(store, base_t_oid, file_path, None)
                commit_oid = commit_on_branch(store, repo, branch, [b_tip], new_t_oid, message)

                return 200, {
                    "content": None,
                    "commit": {
                        "sha": commit_oid
                    }
                }
            return 404, {"message": "Not Found"}

        if sub == ["issues"]:
            if method == "GET":
                state_filter = q.get("state", "open")
                direction = q.get("direction", "desc")
                issues = [i for i in repo.issues.values() if state_filter == "all" or i.state == state_filter]
                issues.sort(key=lambda i: i.created_at, reverse=(direction == "desc"))
                return 200, [make_issue_obj(store, repo, issue, base_url) for issue in issues]
            elif method == "POST":
                title = req_body.get("title")
                if not title:
                    return 422, {"message": "Title is required"}
                issue_body = req_body.get("body", "")
                label_names = req_body.get("labels", [])
                for ln in label_names:
                    if ln not in repo.labels:
                        return 422, {"message": f"Label '{ln}' does not exist"}

                number = repo.next_number
                repo.next_number += 1
                db_id = store.next_issue_db_id
                store.next_issue_db_id += 1

                now_str = store.now_iso()
                issue = Issue(
                    number=number,
                    title=title,
                    body=issue_body,
                    state="open",
                    labels=label_names,
                    author=store.get_owner_login(),
                    created_at=now_str,
                    updated_at=now_str,
                    db_id=db_id,
                    comments_count=0
                )
                repo.issues[number] = issue
                return 201, make_issue_obj(store, repo, issue, base_url)
            return 404, {"message": "Not Found"}

        if len(sub) >= 2 and sub[0] == "issues":
            try:
                n = int(sub[1])
            except ValueError:
                return 404, {"message": "Not Found"}

            issue = repo.get_issue(n)
            if not issue:
                return 404, {"message": "Not Found"}

            issue_sub = sub[2:]
            if not issue_sub:
                if method == "GET":
                    return 200, make_issue_obj(store, repo, issue, base_url)
                elif method == "PATCH":
                    if "title" in req_body:
                        issue.title = req_body["title"]
                    if "body" in req_body:
                        issue.body = req_body["body"]
                    if "state" in req_body:
                        ns = req_body["state"]
                        if ns in ("open", "closed"):
                            issue.state = ns
                            if ns == "closed":
                                issue.closed_at = store.now_iso()
                            else:
                                issue.closed_at = None
                    if "labels" in req_body:
                        lbls = req_body["labels"]
                        if isinstance(lbls, list):
                            for ln in lbls:
                                if ln not in repo.labels:
                                    return 422, {"message": f"Label '{ln}' does not exist"}
                            issue.labels = lbls
                    issue.updated_at = store.now_iso()
                    return 200, make_issue_obj(store, repo, issue, base_url)
                return 404, {"message": "Not Found"}

            if issue_sub == ["comments"]:
                if method == "GET":
                    comments = repo.comments.get(n, [])
                    return 200, [make_comment_obj(store, repo, n, c, base_url) for c in comments]
                elif method == "POST":
                    c_body = req_body.get("body", "")
                    c_id = store.next_comment_db_id
                    store.next_comment_db_id += 1
                    now_str = store.now_iso()
                    comment = Comment(
                        id=c_id,
                        body=c_body,
                        author=store.get_owner_login(),
                        created_at=now_str,
                        updated_at=now_str,
                        issue_number=n
                    )
                    if n not in repo.comments:
                        repo.comments[n] = []
                    repo.comments[n].append(comment)
                    return 201, make_comment_obj(store, repo, n, comment, base_url)
                return 404, {"message": "Not Found"}

            if issue_sub == ["labels"]:
                if method == "GET":
                    return 200, [make_label_obj(store, repo, repo.labels[ln]) for ln in issue.labels if ln in repo.labels]
                elif method == "POST":
                    # add labels
                    new_lbls = req_body if isinstance(req_body, list) else req_body.get("labels", [])
                    if isinstance(new_lbls, str):
                        new_lbls = [new_lbls]
                    for ln in new_lbls:
                        if ln not in repo.labels:
                            return 422, {"message": f"Label '{ln}' does not exist"}
                        if ln not in issue.labels:
                            issue.labels.append(ln)
                    return 200, [make_label_obj(store, repo, repo.labels[ln]) for ln in issue.labels if ln in repo.labels]
                elif method == "PUT":
                    # set labels
                    new_lbls = req_body if isinstance(req_body, list) else req_body.get("labels", [])
                    for ln in new_lbls:
                        if ln not in repo.labels:
                            return 422, {"message": f"Label '{ln}' does not exist"}
                    issue.labels = list(new_lbls)
                    return 200, [make_label_obj(store, repo, repo.labels[ln]) for ln in issue.labels if ln in repo.labels]
                elif method == "DELETE":
                    # remove all labels
                    issue.labels = []
                    return 200, []
                return 404, {"message": "Not Found"}

            if len(issue_sub) == 2 and issue_sub[0] == "labels":
                lbl_name = issue_sub[1]
                if method == "DELETE":
                    if lbl_name in issue.labels:
                        issue.labels.remove(lbl_name)
                    return 200, [make_label_obj(store, repo, repo.labels[ln]) for ln in issue.labels if ln in repo.labels]
                return 404, {"message": "Not Found"}

        if sub == ["labels"]:
            if method == "GET":
                return 200, [make_label_obj(store, repo, l, base_url) for l in repo.labels.values()]
            elif method == "POST":
                name = req_body.get("name")
                if not name:
                    return 422, {"message": "Name is required"}
                if name in repo.labels:
                    return 422, {"message": "Label already exists"}
                color = req_body.get("color", "ededed").replace("#", "")
                desc = req_body.get("description", "")
                label = Label(name=name, color=color, description=desc)
                repo.labels[name] = label
                return 201, make_label_obj(store, repo, label, base_url)
            return 404, {"message": "Not Found"}

        if len(sub) == 2 and sub[0] == "labels":
            lname = sub[1]
            if method == "GET":
                if lname not in repo.labels:
                    return 404, {"message": "Not Found"}
                return 200, make_label_obj(store, repo, repo.labels[lname], base_url)
            elif method == "PATCH":
                if lname not in repo.labels:
                    return 404, {"message": "Not Found"}
                label = repo.labels[lname]
                if "name" in req_body:
                    new_lname = req_body["name"]
                    if new_lname != lname:
                        if new_lname in repo.labels:
                            return 422, {"message": "Label already exists"}
                        repo.labels.pop(lname)
                        label.name = new_lname
                        repo.labels[new_lname] = label
                        lname = new_lname
                if "color" in req_body:
                    label.color = req_body["color"].replace("#", "")
                if "description" in req_body:
                    label.description = req_body["description"]
                return 200, make_label_obj(store, repo, label, base_url)
            elif method == "DELETE":
                if lname in repo.labels:
                    repo.labels.pop(lname)
                return 204, None
            return 404, {"message": "Not Found"}

    return 404, {"message": "Not Found"}
