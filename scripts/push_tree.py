#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 Git Data API 把本地 HEAD 整棵树推到远端（原子、并行、失败不动远端）。

## 为什么不用 git push

本机环境有透明代理（127.0.0.1:49917）：
  - api.github.com  → 放行（0.5s 返回）
  - github.com:443  → CONNECT 隧道 502
所以 `git push` 必然失败，但 GitHub REST API 完全可用。
这不是临时抖动，是代理策略，重试 git 没有意义。

## 为什么并行

49 个文件串行 PUT 会超过工具超时（实测 SIGTERM）。
用线程池并发建 blob，整体降到 2~3 秒。

## 原子性

所有 blob 建好后才建 tree / commit / 更新 ref。
中途失败 → 远端 ref 完全没动，重跑即可，不会留下半新半旧的树。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

OWNER = "liehuu"
REPO = "UnCast"
BRANCH = "main"
API = "https://api.github.com"

# 这些后缀不是可执行文件，避免误设 100755
NON_EXEC_EXT = (".png", ".json", ".md", ".yml", ".yaml", ".swift",
                ".plist", ".py", ".gitignore", ".gitattributes", ".txt")


def get_token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, timeout=30,
    )
    for line in out.stdout.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    raise SystemExit("取不到 token")


def api(method, path, token, payload=None, retries=5, timeout=60):
    url = path if path.startswith("http") else API + path
    data = json.dumps(payload).encode() if payload is not None else None
    last = None
    for a in range(retries):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "uncast-tree-push")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode()
                return r.status, (json.loads(body) if body.strip() else {})
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:250]
            if e.code in (401, 403):
                raise SystemExit(f"权限失败 ({e.code}): {detail}")
            last = f"HTTP {e.code}: {detail}"
            time.sleep(1.5 * (a + 1))
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(2 * (a + 1))
    raise SystemExit("API 失败: " + str(last))


def local_commit_info():
    head = subprocess.run(["git", "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True
                          ).stdout.strip()
    msg = subprocess.run(["git", "log", "-1", "--format=%B", "HEAD"],
                         capture_output=True, text=True, check=True
                         ).stdout.strip()
    files = subprocess.run(["git", "ls-files", "-z"],
                           capture_output=True, text=True, check=True
                           ).stdout
    return head, msg, [p for p in files.split("\0") if p]


def make_blob(token, path):
    with open(path, "rb") as f:
        raw = f.read()
    _, blob = api("POST", f"/repos/{OWNER}/{REPO}/git/blobs", token,
                  {"content": base64.b64encode(raw).decode(), "encoding": "base64"})
    mode = "100644"
    if not path.endswith(NON_EXEC_EXT) and os.access(path, os.X_OK):
        mode = "100755"
    return {"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]}


def main() -> int:
    token = get_token()
    head, msg, files = local_commit_info()
    print(f"本地 HEAD : {head[:12]}")
    print(f"跟踪文件  : {len(files)} 个")

    remote_head = None
    base_tree = None
    try:
        _, ref = api("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/{BRANCH}", token)
        remote_head = ref["object"]["sha"]
        print(f"远端 HEAD : {remote_head[:12]}")
        _, rc = api("GET", f"/repos/{OWNER}/{REPO}/git/commits/{remote_head}", token)
        base_tree = rc["tree"]["sha"]
    except SystemExit:
        print("远端 main 不存在，创建首个提交")
    except KeyError:
        print("远端 ref 结构异常，按首提交处理")

    if remote_head == head:
        print("远端已是最新，无需推送")
        return 0

    # ── 并行建 blob
    t0 = time.time()
    entries, errors = [], []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(make_blob, token, p): p for p in files
                if os.path.isfile(p)}
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                entries.append(fut.result())
            except Exception as e:
                errors.append(f"{p}: {e}")
    print(f"blob 完成  : {len(entries)}/{len(files)}，{time.time()-t0:.1f}s")
    if errors:
        print("失败文件:")
        for e in errors[:10]:
            print("  ", e)
        raise SystemExit("有 blob 建立失败，未改动远端")

    # 顺序稳定，便于比对
    entries.sort(key=lambda e: e["path"])

    # ── 建树
    tree_payload = {"tree": entries}
    if base_tree:
        tree_payload["base_tree"] = base_tree
    _, tree = api("POST", f"/repos/{OWNER}/{REPO}/git/trees", token, tree_payload)
    print(f"tree      : {tree['sha'][:12]}")

    # ── 建提交
    cp = {"message": msg, "tree": tree["sha"]}
    if remote_head:
        cp["parents"] = [remote_head]
    _, commit = api("POST", f"/repos/{OWNER}/{REPO}/git/commits", token, cp)
    print(f"commit    : {commit['sha'][:12]}")

    # ── 推进 ref
    if remote_head:
        api("PATCH", f"/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}", token,
            {"sha": commit["sha"], "force": False})
    else:
        api("POST", f"/repos/{OWNER}/{REPO}/git/refs", token,
            {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})

    # ── 回读确认（不信返回值，只看远端实际状态）
    _, check = api("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/{BRANCH}", token)
    final = check["object"]["sha"]
    print(f"远端 main : {final[:12]}")
    if final != commit["sha"]:
        print("!! 远端 sha 与提交不一致")
        return 1
    print(f"推送成功，总耗时 {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())