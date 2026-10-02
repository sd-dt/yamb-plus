#!/usr/bin/env python3
"""GitHub 同步上传（交接文档《GitHub上传途径》的方法）：
REST API blob→tree→commit→ref，幂等（与远端 tree 逐文件对比，只传差异），不走 git push。

用法:
  python3 upload-github.py --dir DIR --repo OWNER/NAME --branch BRANCH --message "msg"

token 来源顺序: --token-file / 环境变量 GITHUB_TOKEN / ~/.github-token
文件清单取自 `git ls-files -s`（目录必须是 git 仓库），保留 644/755 权限位。
"""
import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"


def api(method, path, token, payload=None, retries=3):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(
            API + path, data=data, method=method,
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "User-Agent": "yamb-plus-uploader"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
            return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last = e
        except Exception as e:
            last = e
        time.sleep(3)
    raise last


def git(dirpath, *args):
    out = subprocess.run(["git", "-C", dirpath] + list(args),
                         capture_output=True, check=True, text=True)
    return out.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="本地 git 仓库目录")
    ap.add_argument("--repo", required=True, help="OWNER/NAME")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message", default="sync")
    ap.add_argument("--token-file", default=None)
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        cands = ([args.token_file] if args.token_file else []) + [os.path.expanduser("~/.github-token")]
        for cand in cands:
            if cand and os.path.isfile(cand):
                token = open(cand).read().strip()
                break
    if not token:
        sys.exit("!! 找不到 GitHub token（--token-file / GITHUB_TOKEN / ~/.github-token）")

    dirpath = os.path.abspath(args.dir)
    entries = []
    for line in git(dirpath, "ls-files", "-s").splitlines():
        meta, path = line.split("\t", 1)
        mode, _oid, _stage = meta.split()
        with open(os.path.join(dirpath, path), "rb") as fh:
            data = fh.read()
        sha = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        entries.append({"path": path, "mode": mode, "sha": sha, "data": data})
    print("本地文件 %d 个" % len(entries))

    head = {}
    try:
        head = api("GET", "/repos/%s/branches/%s" % (args.repo, args.branch), token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("远端分支不存在（空仓库），将全量首传")
        else:
            raise
    remote_tree = {}
    if head.get("commit", {}).get("commit", {}).get("tree", {}).get("sha"):
        tree_sha = head["commit"]["commit"]["tree"]["sha"]
        t = api("GET", "/repos/%s/git/trees/%s?recursive=1" % (args.repo, tree_sha), token)
        remote_tree = {i["path"]: i["sha"] for i in t.get("tree", []) if i["type"] == "blob"}

    local = {e["path"]: e["sha"] for e in entries}
    changed = [e for e in entries if remote_tree.get(e["path"]) != e["sha"]]
    removed = [p for p in remote_tree if p not in local]
    if head and not changed and not removed:
        print("远端已同步，无需提交")
        return
    if not head:
        changed = entries
    print("需要上传 %d 个，删除 %d 个" % (len(changed), len(removed)))

    tree_items = []
    for e in changed:
        r = api("POST", "/repos/%s/git/blobs" % args.repo, token,
                {"content": base64.b64encode(e["data"]).decode("ascii"), "encoding": "base64"})
        tree_items.append({"path": e["path"], "mode": e["mode"], "type": "blob", "sha": r["sha"]})
    for p in removed:
        tree_items.append({"path": p, "mode": "100644", "type": "blob", "sha": None})

    tree_payload = {"tree": tree_items}
    if head:
        tree_payload["base_tree"] = head["commit"]["commit"]["tree"]["sha"]
    tree = api("POST", "/repos/%s/git/trees" % args.repo, token, tree_payload)

    commit_payload = {"message": args.message, "tree": tree["sha"]}
    if head:
        commit_payload["parents"] = [head["commit"]["sha"]]
    commit = api("POST", "/repos/%s/git/commits" % args.repo, token, commit_payload)

    if head:
        api("PATCH", "/repos/%s/git/refs/heads/%s" % (args.repo, args.branch), token,
            {"sha": commit["sha"], "force": True})
    else:
        api("POST", "/repos/%s/git/refs" % args.repo, token,
            {"ref": "refs/heads/%s" % args.branch, "sha": commit["sha"]})
    print("已提交 %s -> %s@%s" % (commit["sha"][:10], args.repo, args.branch))


if __name__ == "__main__":
    main()
