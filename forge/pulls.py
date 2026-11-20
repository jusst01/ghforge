"""Pull request management and REST/GraphQL helpers for the forge."""

import base64
import datetime
import difflib
import json
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from forge.objects import Commit, Tree, TreeEntry, Blob, parse_object, format_ident, oid_of
    from forge.store import Store, Repo, Pull, Issue, gid, parse_gid
    from forge.rest import (
        read_tree_files,
        apply_path_change,
        commit_on_branch,
        make_repo_obj,
        make_user_obj,
        make_label_obj,
    )
except ImportError:
    from objects import Commit, Tree, TreeEntry, Blob, parse_object, format_ident, oid_of
    from store import Store, Repo, Pull, Issue, gid, parse_gid
    from rest import (
        read_tree_files,
        apply_path_change,
        commit_on_branch,
        make_repo_obj,
        make_user_obj,
        make_label_obj,
    )


def _commit_tree_and_parents(store: Any, oid: Optional[str]) -> Tuple[Optional[str], List[str]]:
    if not oid or not store.has(oid):
        return None, []
    try:
        obj_type, payload = store.get(oid)
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
    except Exception:
        return None, []


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


def pr_files(store: Any, owner: str, repo: Any, pr: Any) -> List[Dict[str, Any]]:
    if isinstance(repo, str):
        repo_obj = store.get_repo(repo)
    else:
        repo_obj = repo
    if not repo_obj:
        return []

    head_tip = pr.head_sha or repo_obj.get_ref(f"refs/heads/{pr.head_ref}")
    base_tip = pr.base_sha or repo_obj.get_ref(f"refs/heads/{pr.base_ref}")
    mb = _merge_base(store, base_tip, head_tip)
    base_tree = _tree_of(store, mb)
    head_tree = _tree_of(store, head_tip)

    base_files = read_tree_files(store, base_tree)
    head_files = read_tree_files(store, head_tree)

    files_list = []
    login = store.get_owner_login() or owner

    for path in sorted(set(base_files) | set(head_files)):
        b_entry = base_files.get(path)
        h_entry = head_files.get(path)
        if b_entry == h_entry:
            continue

        if b_entry is None:
            status = "added"
            _, h_oid = h_entry
            sha = h_oid
        elif h_entry is None:
            status = "removed"
            sha = None
        else:
            status = "modified"
            _, h_oid = h_entry
            sha = h_oid

        additions = 0
        deletions = 0
        try:
            old_lines = []
            if b_entry is not None:
                _, b_oid = b_entry
                if store.has(b_oid):
                    otype, payload = store.get(b_oid)
                    if otype == "blob":
                        old_lines = payload.decode("utf-8", errors="ignore").splitlines()
            new_lines = []
            if h_entry is not None:
                _, h_oid = h_entry
                if store.has(h_oid):
                    otype, payload = store.get(h_oid)
                    if otype == "blob":
                        new_lines = payload.decode("utf-8", errors="ignore").splitlines()

            diff = list(difflib.ndiff(old_lines, new_lines))
            additions = sum(1 for line in diff if line.startswith("+ "))
            deletions = sum(1 for line in diff if line.startswith("- "))
        except Exception:
            pass

        changes = additions + deletions

        blob_url = f"/api/v3/repos/{login}/{repo_obj.name}/git/blobs/{sha}" if sha else ""
        raw_url = f"/raw/{repo_obj.name}/{pr.head_ref}/{path}"
        contents_url = f"/api/v3/repos/{login}/{repo_obj.name}/contents/{path}"

        files_list.append({
            "sha": sha,
            "filename": path,
            "status": status,
            "additions": additions,
            "deletions": deletions,
            "changes": changes,
            "blob_url": blob_url,
            "raw_url": raw_url,
            "contents_url": contents_url
        })

    return files_list


def make_pull_obj(store: Any, repo: Any, pull: Any) -> Dict[str, Any]:
    login = store.get_owner_login() or "owner"
    repo_name = repo.name
    url = f"/api/v3/repos/{login}/{repo_name}/pulls/{pull.number}"
    html_url = f"/{login}/{repo_name}/pull/{pull.number}"
    repo_url = f"/api/v3/repos/{login}/{repo_name}"

    head_tip = pull.head_sha or repo.get_ref(f"refs/heads/{pull.head_ref}") or ""
    base_tip = pull.base_sha or repo.get_ref(f"refs/heads/{pull.base_ref}") or ""

    files = pr_files(store, login, repo, pull)
    changed_files_count = len(files)
    additions = sum(f["additions"] for f in files)
    deletions = sum(f["deletions"] for f in files)

    mb = _merge_base(store, base_tip, head_tip)
    commits_count = 0
    if head_tip and mb:
        curr = head_tip
        visited_commits = set()
        while curr and curr != mb and curr not in visited_commits:
            visited_commits.add(curr)
            commits_count += 1
            _, parents = _commit_tree_and_parents(store, curr)
            curr = parents[0] if parents else None

    mergeable = True
    if not pull.merged and head_tip and base_tip:
        try:
            mb_tree = _tree_of(store, mb)
            ours_tree = _tree_of(store, base_tip)
            theirs_tree = _tree_of(store, head_tip)
            _, conflicts = _three_way_merge(store, mb_tree, ours_tree, theirs_tree)
            if conflicts:
                mergeable = False
        except Exception:
            mergeable = None

    repo_obj = make_repo_obj(store, repo, "")

    return {
        "id": pull.db_id,
        "node_id": gid("PullRequest", pull.db_id),
        "url": url,
        "html_url": html_url,
        "diff_url": f"{url}.diff",
        "patch_url": f"{url}.patch",
        "issue_url": f"/api/v3/repos/{login}/{repo_name}/issues/{pull.number}",
        "number": pull.number,
        "state": pull.state,
        "locked": False,
        "title": pull.title,
        "body": pull.body or "",
        "user": make_user_obj(store),
        "labels": [make_label_obj(store, repo, repo.labels[ln]) for ln in pull.labels if ln in repo.labels],
        "assignee": None,
        "assignees": [],
        "requested_reviewers": [],
        "requested_teams": [],
        "milestone": None,
        "created_at": pull.created_at,
        "updated_at": pull.updated_at,
        "closed_at": pull.closed_at,
        "merged_at": pull.merged_at,
        "merge_commit_sha": pull.merge_commit_sha,
        "merged": bool(pull.merged),
        "mergeable": mergeable,
        "rebaseable": None,
        "mergeable_state": "clean" if mergeable else "dirty",
        "draft": bool(getattr(pull, "draft", False)),
        "head": {
            "label": f"{login}:{pull.head_ref}",
            "ref": pull.head_ref,
            "sha": head_tip,
            "user": make_user_obj(store),
            "repo": repo_obj
        },
        "base": {
            "label": f"{login}:{pull.base_ref}",
            "ref": pull.base_ref,
            "sha": base_tip,
            "user": make_user_obj(store),
            "repo": repo_obj
        },
        "author_association": "OWNER",
        "auto_merge": None,
        "active_lock_reason": None,
        "merged_by": make_user_obj(store) if pull.merged else None,
        "comments": repo.get_comments_count(pull.number),
        "review_comments": 0,
        "maintainer_can_modify": False,
        "commits": commits_count,
        "additions": additions,
        "deletions": deletions,
        "changed_files": changed_files_count
    }


def merge_pull(store: Any, owner: str, repo: Any, pr: Any, method: str = "merge", body: Optional[Dict[str, Any]] = None, query: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
    if isinstance(repo, str):
        repo_obj = store.get_repo(repo)
    else:
        repo_obj = repo
    if not repo_obj or not pr:
        return 404, {"message": "Not Found"}

    if pr.state != "open" or pr.merged:
        return 405, {"message": "Pull request cannot be merged"}

    head_tip = pr.head_sha or repo_obj.get_ref(f"refs/heads/{pr.head_ref}")
    base_tip = repo_obj.get_ref(f"refs/heads/{pr.base_ref}") or pr.base_sha
    if not head_tip or not base_tip or not store.has(head_tip) or not store.has(base_tip):
        return 405, {"message": "Head or base tip not found"}

    mb = _merge_base(store, base_tip, head_tip)

    if method not in ("merge", "squash"):
        return 422, {"message": "Invalid merge method"}

    body_dict = body if isinstance(body, dict) else {}
    query_dict = query if isinstance(query, dict) else {}

    if mb == base_tip and method == "merge":
        # Fast forward
        merge_commit_sha = head_tip
        repo_obj.set_ref(f"refs/heads/{pr.base_ref}", head_tip)
    else:
        mb_tree = _tree_of(store, mb) if mb else store.put("tree", b"")
        base_tree = _tree_of(store, base_tip)
        head_tree = _tree_of(store, head_tip)
        merged_files, conflicts = _three_way_merge(store, mb_tree, base_tree, head_tree)
        if conflicts:
            return 409, {"message": f"Merge conflict in paths: {', '.join(conflicts)}"}
        merged_tree_oid = _build_tree(store, merged_files)

        commit_title = body_dict.get("commit_title")
        commit_message = body_dict.get("commit_message")

        if method == "squash":
            parents = [base_tip]
            msg = commit_title or f"{pr.title} (#{pr.number})"
            if commit_message:
                msg += f"\n\n{commit_message}"
            elif pr.body:
                msg += f"\n\n{pr.body}"
        else:
            parents = [base_tip, head_tip]
            msg = commit_title or f"Merge pull request #{pr.number} from {pr.head_ref}\n\n{pr.title}"
            if commit_message:
                msg += f"\n\n{commit_message}"

        merge_commit_sha = commit_on_branch(store, repo_obj, pr.base_ref, parents, merged_tree_oid, msg)

    now = store.now_iso()
    with store.lock:
        pr.merged = True
        pr.state = "closed"
        pr.merged_at = now
        pr.closed_at = now
        pr.updated_at = now
        pr.merge_commit_sha = merge_commit_sha

    delete_branch = query_dict.get("delete_branch") or body_dict.get("delete_branch") or body_dict.get("delete_branch_on_merge")
    if delete_branch and str(delete_branch).lower() in ("true", "1", "yes"):
        repo_obj.delete_ref(f"refs/heads/{pr.head_ref}")

    return 200, {
        "sha": merge_commit_sha,
        "merged": True,
        "message": "Pull Request successfully merged"
    }


def handle_pulls(store: Any, owner: str, repo: Any, method: str, rest_seg: List[str], query: Any, body: Any) -> Tuple[int, Any]:
    if isinstance(repo, str):
        repo_obj = store.get_repo(repo)
    else:
        repo_obj = repo
    if not repo_obj:
        return 404, {"message": "Not Found"}

    if isinstance(body, bytes):
        try:
            req_body = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            return 400, {"message": "Malformed JSON"}
    elif isinstance(body, str):
        try:
            req_body = json.loads(body) if body else {}
        except Exception:
            return 400, {"message": "Malformed JSON"}
    elif isinstance(body, dict):
        req_body = body
    else:
        req_body = {}

    def get_q(q, key, default=""):
        if not q:
            return default
        val = q.get(key, default)
        if isinstance(val, list):
            return val[0] if val else default
        return val if val is not None else default

    # List or Create pulls
    if not rest_seg:
        if method == "GET":
            state_filter = get_q(query, "state", "open").lower()
            pulls_list = []
            for p in sorted(repo_obj.pulls.values(), key=lambda x: x.number):
                if state_filter == "open":
                    if p.state == "open" and not p.merged:
                        pulls_list.append(make_pull_obj(store, repo_obj, p))
                elif state_filter == "closed":
                    if p.state == "closed" or p.merged:
                        pulls_list.append(make_pull_obj(store, repo_obj, p))
                else:  # all
                    pulls_list.append(make_pull_obj(store, repo_obj, p))
            return 200, pulls_list

        elif method == "POST":
            title = req_body.get("title")
            if not isinstance(title, str) or not title.strip():
                return 422, {"message": "Validation Failed: title can't be blank"}
            head = req_body.get("head")
            if not head:
                return 422, {"message": "Head branch is required"}
            if ":" in head:
                head = head.split(":", 1)[1]

            base = req_body.get("base") or repo_obj.default_branch

            head_ref_key = f"refs/heads/{head}"
            base_ref_key = f"refs/heads/{base}"

            head_sha = repo_obj.get_ref(head_ref_key)
            base_sha = repo_obj.get_ref(base_ref_key)

            if not head_sha and not store.has(head):
                return 422, {"message": f"Head branch {head} not found"}
            if not base_sha and not store.has(base):
                return 422, {"message": f"Base branch {base} not found"}

            for existing in repo_obj.pulls.values():
                if existing.state == "open" and existing.head_ref == head and existing.base_ref == base:
                    return 422, {"message": "A pull request already exists for this head and base."}

            with store.lock:
                number = repo_obj.next_number
                repo_obj.next_number += 1
                db_id = store.next_pull_db_id
                store.next_pull_db_id += 1
                now = store.now_iso()
                draft = bool(req_body.get("draft", False))
                pull = Pull(
                    number=number,
                    title=title,
                    body=req_body.get("body", ""),
                    state="open",
                    head_ref=head,
                    base_ref=base,
                    head_sha=head_sha or head,
                    base_sha=base_sha or base,
                    created_at=now,
                    updated_at=now,
                    db_id=db_id,
                    draft=draft
                )
                repo_obj.pulls[number] = pull

            return 201, make_pull_obj(store, repo_obj, pull)

        return 404, {"message": "Not Found"}

    # Get, Patch, Files, Merge specific pull
    try:
        n = int(rest_seg[0])
    except ValueError:
        return 404, {"message": "Not Found"}

    pull = repo_obj.get_pull(n)
    if not pull:
        return 404, {"message": "Not Found"}

    if len(rest_seg) == 1:
        if method == "GET":
            return 200, make_pull_obj(store, repo_obj, pull)
        elif method == "PATCH":
            now = store.now_iso()
            with store.lock:
                if "title" in req_body:
                    pull.title = req_body["title"]
                if "body" in req_body:
                    pull.body = req_body["body"]
                if "draft" in req_body:
                    pull.draft = bool(req_body["draft"])
                if "state" in req_body:
                    new_state = req_body["state"]
                    if new_state in ("open", "closed"):
                        if pull.state != new_state:
                            pull.state = new_state
                            if new_state == "closed":
                                pull.closed_at = now
                            elif new_state == "open":
                                pull.closed_at = None
                                pull.merged = False
                                pull.merged_at = None
                pull.updated_at = now
            return 200, make_pull_obj(store, repo_obj, pull)
        return 404, {"message": "Not Found"}

    if len(rest_seg) == 2:
        action = rest_seg[1]
        if action == "files" and method == "GET":
            return 200, pr_files(store, owner, repo_obj, pull)
        elif action == "merge" and method == "PUT":
            method_type = req_body.get("merge_method", "merge")
            return merge_pull(store, owner, repo_obj, pull, method_type, req_body, query)

    return 404, {"message": "Not Found"}
