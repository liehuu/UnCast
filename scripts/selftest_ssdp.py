#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_ssdp.py 的变异测试：证明 34 条断言真的能失败。

## 为什么必须有这个文件

verify_ssdp.py 是静态结构检查，最容易退化成永真断言：
正则放宽一点、关键字写错一个字符，报告依然 34/34 全绿，
但什么都没在检查。全绿只有在「能被破坏」的前提下才有意义。

## 注入清单

A 组：**回放本次修复前的真实缺陷**（最重要 —— 这些是设备上真出过的）
B 组：逐条破坏，确认对应断言会红
C 组：反向确认 —— 合法变体不能被误杀（防「把守卫改成摆设」）

每处注入后立即还原，最后复跑确认回到全绿。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "UnCast", "DLNA", "SSDPResponder.swift")
SVC = os.path.join(ROOT, "UnCast", "DLNA", "DLNAService.swift")
HTTP = os.path.join(ROOT, "UnCast", "DLNA", "HTTPServer.swift")
VERIFY = os.path.join(ROOT, "scripts", "verify_ssdp.py")
PY = sys.executable


def run_verify() -> tuple[int, list[str]]:
    r = subprocess.run([PY, VERIFY], capture_output=True, text=True, cwd=ROOT)
    fails = [ln.strip()[2:] for ln in r.stdout.splitlines()
             if ln.strip().startswith("- ")]
    # 崩溃与「检查不通过」要区分：NameError 也会 rc!=0，但那是测试自己坏了。
    crashed = "Traceback" in r.stderr or r.returncode not in (0, 1)
    if crashed:
        fails.append("__CRASH__:" + r.stderr.strip().splitlines()[-1:][0]
                     if r.stderr.strip() else "__CRASH__")
    return r.returncode, fails


def patch(path: str, old: str, new: str, label: str,
          expect: str | tuple[str, ...], results: list):
    """改一处 → 跑校验 → 还原。

    expect 允许多个候选：一处破坏常让「上游」断言先红
    （删掉整个函数后，报的是「函数不存在」而不是函数内部那条）。
    命中任一即证明门禁确实盯着这个缺陷。
    """
    keys = (expect,) if isinstance(expect, str) else expect
    src = open(path, encoding="utf-8").read()
    if old not in src:
        results.append((label, False, f"注入锚点未找到: {old[:50]!r}"))
        return
    backup = src
    try:
        open(path, "w", encoding="utf-8", newline="\n").write(
            src.replace(old, new, 1))
        rc, fails = run_verify()
        hit_key = next((k for k in keys if any(k in f for f in fails)), None)
        hit = rc != 0 and hit_key is not None
        detail = (f"rc={rc}, 命中「{hit_key}」" if hit
                  else f"rc={rc}, 未命中预期（报出 {fails[:2]}）")
        results.append((label, hit, detail))
    finally:
        open(path, "w", encoding="utf-8", newline="\n").write(backup)


def main() -> int:
    for p in (SRC, SVC, HTTP):
        if not os.path.isfile(p):
            print(f"找不到 {p}")
            return 2

    shutil.copy2(SRC, SRC + ".orig")
    shutil.copy2(SVC, SVC + ".orig")
    shutil.copy2(HTTP, HTTP + ".orig")

    rc, fails = run_verify()
    print(f"[基线] rc={rc}, fails={len(fails)}")
    if rc != 0:
        print("基线不绿，无法做变异测试")
        shutil.move(SRC + ".orig", SRC)
        shutil.move(SVC + ".orig", SVC)
        shutil.move(HTTP + ".orig", HTTP)
        return 1

    results: list = []

    # ══════════════════════════════════════════════════════════
    print("\n== A. 回放本次修复前的真实缺陷 ==")
    # ══════════════════════════════════════════════════════════

    # A1: ST 原样回显 —— 本次最可能的根因
    patch(SRC,
          """        if s == "ssdp:all" {
            // UPnP 1.1 §1.2.3：ssdp:all 要为**每一类通告**各回一条，
            // 其中包括裸 UDN（ST/USN 都是 uuid:xxx）。
            // 少数基于 Cling 的老客户端正是靠这条认设备的，不能省。
            return ["uuid:\\(config.uuid)"] + Self.searchTargets
        }""",
          """        if s == "ssdp:all" { return [st] }""",
          "A1 回放：ssdp:all 原样回显 ST",
          ("不回显 st 本身", "展开为多个 target", "回复含裸 UDN"),
          results)

    # A2: 只回一条 MediaRenderer（最常见的不完整实现）
    patch(SRC,
          'return ["uuid:\\(config.uuid)"] + Self.searchTargets',
          'return ["urn:schemas-upnp-org:device:MediaRenderer:1"]',
          "A2 回放：ssdp:all 只回 MediaRenderer 一条",
          ("展开为多个 target", "回复含裸 UDN"),
          results)

    # A3: 没有周期通告（只在启动时发一次）
    patch(SRC,
          "        startNotifyLoop()\n",
          "        // 注入：不启动周期通告\n",
          "A3 回放：去掉周期通告循环",
          "start() 内启动了通告循环",
          results)

    # A4: BOOTID 写死 1
    patch(SRC,
          '"BOOTID.UPNP.ORG: \\(Self.bootID)"',
          '"BOOTID.UPNP.ORG: 1"',
          "A4 回放：BOOTID 硬编码为 1",
          "BOOTID 不由字面量",
          results)

    # A5: 裸 UDN 也加 :: 后缀（幽灵设备）
    patch(SRC,
          """        if target.lowercased().hasPrefix("uuid:") { return target }
        return "uuid:\\(config.uuid)::\\(target)\"""",
          """        return "uuid:\\(config.uuid)::\\(target)\"""",
          "A5 回放：裸 UDN 被加上 :: 后缀",
          "裸 UDN 不加",
          results)

    # ══════════════════════════════════════════════════════════
    print("\n== B. 逐条破坏 ==")
    # ══════════════════════════════════════════════════════════

    # B1: 删掉一个必需 target
    patch(SRC,
          '        "urn:schemas-upnp-org:service:ConnectionManager:1"\n    ]',
          '    ]',
          "B1 删掉 ConnectionManager target",
          "含 urn:schemas-upnp-org:service:ConnectionManager:1",
          results)

    # B2: maxAge 与 notifyInterval 脱钩
    patch(SRC,
          "static let notifyInterval: TimeInterval = Double(maxAge) / 2",
          "static let notifyInterval: TimeInterval = 1800",
          "B2 notifyInterval 写成独立常量",
          "notifyInterval 由 maxAge 推导",
          results)

    # B3: CACHE-CONTROL 改回硬编码
    patch(SRC,
          '"CACHE-CONTROL: max-age=\\(Self.maxAge)"',
          '"CACHE-CONTROL: max-age=1800"',
          "B3 CACHE-CONTROL 硬编码",
          "CACHE-CONTROL 不再硬编码",
          results)

    # B4: 通告循环放在 isRunning 之前（启动即退出）
    patch(SRC,
          """        self.socketFD = fd
        isRunning = true""",
          """        self.socketFD = fd
        startNotifyLoop()
        isRunning = true""",
          "B4 通告循环早于 isRunning 置真",
          ("通告循环在 isRunning 置真之后启动",),
          results)

    # B5: 内层 while 去掉 isRunning（stop 后要等满周期才退出）
    patch(SRC,
          "while slept < Self.notifyInterval && self.isRunning {",
          "while slept < Self.notifyInterval {",
          "B5 内层循环不检查 isRunning",
          "两层 while 都检查 isRunning",
          results)

    # B5b: 外层 while 去掉 isRunning（stop 后永不停）
    patch(SRC,
          "while self.isRunning {\n                self.announceAlive()",
          "while true {\n                self.announceAlive()",
          "B5b 外层循环不检查 isRunning",
          "两层 while 都检查 isRunning",
          results)

    # B6: 回复发到 1900 组播而不是来源端口
    patch(SRC,
          "sendUDP(payload, to: ip, port: port)",
          "sendUDP(payload, to: ip, port: 1900)",
          "B6 回复发到 1900 而非来源端口",
          "回复发给请求方 IP:端口",
          results)

    # B7: 只回一条（for 循环退化成单次）
    patch(SRC,
          """            for t in targets {
                respondUnicast(to: target, port: targetPort, st: t)
            }""",
          """            if let t = targets.first {
                respondUnicast(to: target, port: targetPort, st: t)
            }""",
          "B7 只回第一条 target",
          "M-SEARCH 分支逐条回多目标",
          results)

    # B8: DLNAService 又重复发一次通告
    patch(SVC,
          """            // 启动通告由 SSDPResponder 内部的 notify 循环负责（含 0.3s 首次延迟），
            // 这里不再重复发一次 —— 重复发只会让局域网多收 5 条无意义报文。""",
          """            responder.announceAlive()""",
          "B8 DLNAService 重复发通告",
          "DLNAService 不再重复发启动通告",
          results)

    # B9: HTTP 的 Server 头改回独立字面量（两处不一致 → 客户端当两台设备）
    patch(HTTP,
          'headers["Server"] = SSDPResponder.serverString',
          'headers["Server"] = "UnCast/1.0 UPnP/1.0 DLNADOC/1.50"',
          "B9 HTTP 的 Server 头另起字面量",
          ("HTTP 的 Server 头复用", "HTTP 侧无残留"),
          results)

    # ══════════════════════════════════════════════════════════
    print("\n== C. 合法变体不能被误杀 ==")
    # ══════════════════════════════════════════════════════════

    # C1: maxAge 改成 900 应该仍然绿（不是写死 1800 才算对）
    patch(SRC,
          "static let maxAge = 1800",
          "static let maxAge = 900",
          "C1 maxAge 改为合法值 900 仍应通过",
          "__NEVER__",  # 期望「不命中任何失败」
          results)
    # C1 的判定要反过来：这一条要求 rc==0
    if results and results[-1][0].startswith("C1"):
        label, _, detail = results[-1]
        ok = detail.startswith("rc=0")
        results[-1] = (label, ok, detail if ok else f"被误杀: {detail}")

    # C2: 注释里出现 ssdp:all 不应影响判定（剥注释的有效性）
    patch(SRC,
          "    /// 单播回复 M-SEARCH。",
          '    /// 错误示例："ST: ssdp:all" 直接回显是错的\n    /// 单播回复 M-SEARCH。',
          "C2 注释含 ssdp:all 不误伤",
          "__NEVER__",
          results)
    if results and results[-1][0].startswith("C2"):
        label, _, detail = results[-1]
        ok = detail.startswith("rc=0")
        results[-1] = (label, ok, detail if ok else f"被误杀: {detail}")

    # ── 汇总 ──
    print()
    ok_all = True
    for label, hit, detail in results:
        print(f"[{'PASS' if hit else 'FAIL'}] {label}  ({detail})")
        ok_all &= hit

    # 还原并复跑
    shutil.move(SRC + ".orig", SRC)
    shutil.move(SVC + ".orig", SVC)
    shutil.move(HTTP + ".orig", HTTP)
    rc, fails = run_verify()
    print(f"\n[还原后复跑] rc={rc}, fails={len(fails)}")
    ok_all &= (rc == 0 and not fails)

    print("\n结论:", "SSDP 契约校验有效（能红能绿，且抓住真实缺陷）"
          if ok_all else "存在永真断言或误杀，必须修")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())