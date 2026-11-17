#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_icon.py 的两条代码路径都要真的跑一遍。

## 为什么需要这个文件

CI run #32 红在一个我自己引入的低级错误：

    check_icon.py: 853  NameError: name 'root' is not defined

原因是 main() 里重构时删掉了局部 `root`，但 `.app` 分支还在用它。
而本机跑 `check_icon.py .` 走的是 `else` 分支（只查源资源），
`.app` 分支**从来没在本机执行过**，所以本地 34/34 全绿、CI 一上来就崩。

这类缺陷的共同形状是：**判据覆盖的分支 ≠ 实际使用的分支**。
「本地绿、CI 红」不是运气问题，是覆盖缺口。本文件就是补这个缺口：
把 `.app` 分支用一个人造的最小包在本地跑起来，并断言它不抛异常。

## 关键断言（这条才是核心）

    stderr 里不许出现 Traceback

因为 NameError 的退出码也是 1，和「检查项不通过」的退出码撞了。
只看 rc 分不清「脚本崩了」和「图标真的不对」——而这两种情况的
处理方式完全不同（改脚本 vs 改资源）。所以必须单独盯 stderr。

用法:
    python3 scripts/selftest_icon_paths.py
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
CHECK = os.path.join(HERE, "check_icon.py")
PY = sys.executable or "python3"


class C:
    def __init__(self):
        self.passes = 0
        self.fails = []

    def check(self, ok, msg, detail=""):
        if ok:
            self.passes += 1
            print(f"  PASS  {msg}")
        else:
            self.fails.append(msg + (f"  [{detail}]" if detail else ""))
            print(f"  FAIL  {msg}" + (f"  [{detail}]" if detail else ""))

    def report(self, title):
        total = self.passes + len(self.fails)
        print(f"\n{title}: {self.passes}/{total}")
        if self.fails:
            print("失败项:")
            for f in self.fails:
                print("  -", f)
            return 1
        return 0


def run_check(target: str, cwd: str | None = None):
    """跑一次 check_icon.py，返回 (rc, stdout, stderr)。"""
    p = subprocess.run(
        [PY, CHECK, target],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return p.returncode, p.stdout, p.stderr


def crashed(rc: int, err: str) -> bool:
    """脚本崩了（而不是检查项不通过）。

    两种信号都算崩溃：有 Traceback，或退出码不是 0/1/2。
    注意 rc==1 不能单独当崩溃信号 —— 那是"检查不通过"的正常退出码。
    """
    return "Traceback" in err or rc not in (0, 1, 2)


def make_fake_app(base: str, ios_plist: bool = False, with_car: bool = True) -> str:
    """造一个最小的 Payload/UnCast.app。

    不追求像素级还原真正的包 —— 这里要验证的是**代码路径能不能跑通**，
    不是图标对不对（那是 check_icon.py 自己的职责）。
    所以 Assets.car 只需要满足两条：
      - 文件 > 1024 字节（脚本有非空断言）
      - 内含 "App Icon" / "Top Shelf Image" 两个明文标识
    """
    app = os.path.join(base, "Payload", "UnCast.app")
    os.makedirs(app, exist_ok=True)

    if with_car:
        # 真实 car 是二进制归档，rendition 名字以明文存在里面，可直接搜。
        # 这里塞够 2KB 填充，保证 size > 1024。
        blob = b"CARC" + b"App Icon" + b"Top Shelf Image" + b"Top Shelf Image Wide"
        blob += b"\x00" * (2048 - len(blob))
        with open(os.path.join(app, "Assets.car"), "wb") as f:
            f.write(blob)

    info = {
        "CFBundleIdentifier": "com.uncasstv.app",
        "CFBundleName": "UnCast",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleSupportedPlatforms": ["AppleTVOS"],
        "CFBundleIcons": (
            {
                # iOS 形式：字典 —— tvOS 读不到，最终表现是主屏白块
                "CFBundlePrimaryIcon": {
                    "CFBundleIconName": "App Icon",
                    "CFBundleIconFiles": ["AppIcon"],
                }
            }
            if ios_plist
            else {
                # tvOS 形式：字符串
                "CFBundlePrimaryIcon": "App Icon",
            }
        ),
        "TVTopShelfImage": {
            "TVTopShelfPrimaryImage": "Top Shelf Image",
            "TVTopShelfPrimaryImageWide": "Top Shelf Image Wide",
        },
    }
    with open(os.path.join(app, "Info.plist"), "wb") as f:
        plistlib.dump(info, f)

    return app


def main() -> int:
    c = C()
    tmp = tempfile.mkdtemp(prefix="uncst_selftest_")
    try:
        # ── A. 仓库根分支（CI Gate 1 走这条）──
        print("\n== A. 仓库根分支 ==")
        rc, out, err = run_check(".")
        c.check(not crashed(rc, err), "A1 check_icon.py . 不崩溃", err.strip()[:400])
        c.check(rc == 0, "A2 源资源全通过", f"rc={rc}")
        c.check("== 1. 源资源尺寸与不透明度 ==" in out, "A3 走到了源资源检查")
        c.check("== 3. 源 Info.plist 图标键" in out, "A4 走到了 plist 结构检查")

        # ── B. .app 分支（CI Gate 2 走这条，run #32 就是这里崩的）──
        print("\n== B. .app 分支（run #32 的崩溃点）==")
        app = make_fake_app(tmp)
        rc, out, err = run_check(app)
        c.check(not crashed(rc, err),
                "B1 check_icon.py <fake>.app 不崩溃（NameError 会在这里现形）",
                err.strip()[:400])
        c.check(rc == 0, "B2 合法包全通过", f"rc={rc}")
        c.check("== 2. 包内 Assets.car 与 Info.plist ==" in out,
                "B3 走到了编译产物检查")
        c.check("== 1. 源资源尺寸与不透明度 ==" in out,
                "B4 .app 分支也查了源资源（源资源从脚本位置推，不从 .app 推）")
        c.check("PASS  Assets.car 内含 'App Icon' 标识" in out,
                "B5 car 内的图标标识被认出来")

        # ── C. 反向：iOS 字典形式必须红 ──
        print("\n== C. 反向 — iOS 字典形式（run #31 的失败原因）==")
        app_ios = make_fake_app(os.path.join(tmp, "ios"), ios_plist=True)
        rc, out, err = run_check(app_ios)
        c.check(not crashed(rc, err), "C1 反向用例也是正常退出，不是崩溃",
                err.strip()[:400])
        c.check(rc == 1, "C2 iOS 字典形式被判不通过", f"rc={rc}")
        c.check("CFBundlePrimaryIcon" in out,
                "C3 失败项点名 CFBundlePrimaryIcon")

        # ── D. 反向：car 缺失时走兜底分支，也不能崩 ──
        print("\n== D. 反向 — 没有 Assets.car ==")
        app_nocar = make_fake_app(os.path.join(tmp, "nocar"), with_car=False)
        rc, out, err = run_check(app_nocar)
        c.check(not crashed(rc, err), "D1 兜底分支不崩溃", err.strip()[:400])
        c.check(rc == 1, "D2 缺 car 被判不通过", f"rc={rc}")
        c.check("兜底" in out, "D3 确实走了兜底分支（不是静默跳过）")

        # ── E. ROOT 必须按脚本位置推，不按 cwd ──
        # 否则在别的目录下调用会去 ./UnCast/... 找资源，全部"目录不存在"。
        print("\n== E. 从无关 cwd 调用 ==")
        rc, out, err = run_check(ROOT, cwd=tmp)
        c.check(not crashed(rc, err), "E1 cwd 无关时不崩溃", err.strip()[:400])
        c.check(rc == 0, "E2 ROOT 解析正确（源资源仍能找到）", f"rc={rc}")

        return c.report("check_icon.py 双路径自检")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())