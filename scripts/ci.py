#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CI 查询 / 产物下载 / 日志解析（uncast 专用小工具）。

## 为什么单独写

本机 `git push` 走不通（代理 502），但 REST API 通。
以下三个操作都需要 token + 重试，散在命令行里容易写错：

  runs    列出最近若干次运行
  jobs    列出某次运行的步骤明细（定位哪一步红）
  logs    拉失败日志（返回的是 zip，必须解压才看得见真实报错）
  artifact 下载 tipa 产物（两段式：带 auth 取 302，再裸取签名 URL）

## 两段式下载的原因

GitHub artifact 的下载接口不直接返回字节，而是 302 到 objects.githubusercontent.com
的临时签名 URL。带着 Authorization 头去访问签名 URL 会被 S3 拒（签名与头冲突），
所以第二次必须去掉 auth —— 这一点踩过一次，记在这里。

用法：
  python scripts/ci.py runs
  python scripts/ci.py jobs <run_id>
  python scripts/ci.py logs <run_id>
  python scripts/ci.py artifact <run_id> <输出路径>
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

OWNER = "liehuu"
REPO = "UnCast"
API = "https://api.github.com"
ARTIFACT_NAME = "UnCast-tipa"


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


def api_get(path: str, token: str, retries: int = 5, timeout: int = 60):
    """返回 (status, json)。网络抖动重试，4xx 直接抛。"""
    last = None
    for a in range(retries):
        req = urllib.request.Request(API + path, method="GET")
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "uncast-ci")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                raise SystemExit(f"HTTP {e.code}: {e.read().decode()[:200]}")
            last = f"HTTP {e.code}"
            time.sleep(1.5 * (a + 1))
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(2 * (a + 1))
    raise SystemExit("API 失败: " + str(last))


def cmd_runs(token: str) -> int:
    _, d = api_get(f"/repos/{OWNER}/{REPO}/actions/runs?per_page=8", token)
    for r in d["workflow_runs"]:
        print(f"{r['id']}  {r['head_sha'][:12]}  {r['status']:<11} "
              f"{str(r['conclusion']):<8} {r['created_at']}")
    return 0


def cmd_jobs(token: str, run_id: str) -> int:
    _, d = api_get(f"/repos/{OWNER}/{REPO}/actions/runs/{run_id}/jobs", token)
    jobs = d.get("jobs", [])
    if not jobs:
        print("没有 job —— 这不是构建失败，是 workflow 没产生 job。")
        print("先验 YAML 语法：`run: |` 块里内容顶格写会截断块标量。")
        return 2
    for j in jobs:
        print(f"[{j['name']}] {j['status']} / {j['conclusion']}")
        for s in j.get("steps", []):
            mark = {"success": "ok  ", "failure": "FAIL", "skipped": "skip"}.get(
                s.get("conclusion") or "", "..  ")
            print(f"  {mark} {s['number']:>2}. {s['name']}")
    return 0


def cmd_logs(token: str, run_id: str) -> int:
    """日志接口返回 zip；直接当文本打印只会看到乱码。"""
    url = f"{API}/repos/{OWNER}/{REPO}/actions/runs/{run_id}/logs"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("User-Agent", "uncast-ci")
    # 日志接口会 302 到签名 URL，urllib 默认会跟随 —— 但带了 auth 头会被拒，
    # 所以手动处理重定向：第二跳去掉 auth。
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        r = opener.open(req, timeout=90)
        data = r.read()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            signed = e.headers["Location"]
            r2 = urllib.request.urlopen(signed, timeout=180)
            data = r2.read()
        else:
            raise SystemExit(f"HTTP {e.code}: {e.read().decode()[:300]}")
    if data[:2] != b"PK":
        print(data.decode(errors="replace"))
        return 0
    zf = zipfile.ZipFile(io.BytesIO(data))
    for name in zf.namelist():
        print("=" * 70)
        print("#", name)
        print("=" * 70)
        print(zf.read(name).decode(errors="replace"))
    return 0


def cmd_artifact(token: str, run_id: str, out_path: str) -> int:
    _, d = api_get(f"/repos/{OWNER}/{REPO}/actions/runs/{run_id}/artifacts", token)
    target = None
    for a in d.get("artifacts", []):
        print(f"artifact: {a['name']}  {a['size_in_bytes']} bytes  "
              f"expired={a['expired']}")
        if a["name"] == ARTIFACT_NAME:
            target = a
    if target is None:
        raise SystemExit(f"没找到 artifact {ARTIFACT_NAME}")
    if target["expired"]:
        raise SystemExit("artifact 已过期")

    # 第一跳：带 auth，拿 302 的 Location
    url = f"{API}/repos/{OWNER}/{REPO}/actions/artifacts/{target['id']}/zip"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("User-Agent", "uncast-ci")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        r = opener.open(req, timeout=90)
        data = r.read()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            signed = e.headers["Location"]
            # 第二跳：**不带** auth，否则签名 URL 会 403
            r2 = urllib.request.urlopen(signed, timeout=300)
            data = r2.read()
        else:
            raise SystemExit(f"HTTP {e.code}: {e.read().decode()[:300]}")

    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    print("zip 内容:", names)
    inner = [n for n in names if n.endswith(".tipa")]
    if not inner:
        raise SystemExit("zip 里没有 .tipa")
    raw = zf.read(inner[0])
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(raw)
    import hashlib
    print(f"写出 {out_path}  {len(raw)} bytes")
    print("sha256", hashlib.sha256(raw).hexdigest())
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    token = get_token()
    if cmd == "runs":
        return cmd_runs(token)
    if cmd == "jobs":
        return cmd_jobs(token, sys.argv[2])
    if cmd == "logs":
        return cmd_logs(token, sys.argv[2])
    if cmd == "artifact":
        return cmd_artifact(token, sys.argv[2], sys.argv[3])
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())