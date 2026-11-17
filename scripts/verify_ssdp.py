#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSDP 发现契约校验。

## 为什么需要这个脚本

「有些 App 搜不到 UnCast」排查下来，根因不在手机端，而在我们自己的
SSDP 报文不符合 UPnP 规范。四个缺陷全都能编译通过、本地也不报错，
只有真机对着某些严苛客户端时才表现为「设备列表里没有」：

1. **ST 回显错误**：收到 `ST: ssdp:all` 就原样回 `ST: ssdp:all`。
   客户端按 ST 匹配设备类型，`ssdp:all` 不是任何设备类型，整条回复被丢弃。
   现象：M-SEARCH 日志里有记录，手机上却什么都没有。
2. **没有周期通告**：只启动时发一次 alive。手机缓存过期（max-age 到点）
   后再开投屏列表就是空的，重启 App 才又能看到 —— 典型「有时候搜不到」。
3. **缺裸 UDN 通告**：UPnP 要求 uuid:xxx 这一条也通告，部分 Cling 系客户端
   只认这条。
4. **BOOTID 写死 1**：App 重启后端口变了，客户端还在用旧缓存连旧端口。

## 判据必须盯源码结构，不能盯「我另写一份逻辑」

本机没有 Swift 工具链，跑不了真实实现。所以这里做**静态契约提取**：
从 .swift 里正则抽出常量与结构，断言它们满足规范。
刻意不写「Python 复刻版 replyTargets」再自测 —— 那只能证明我理解自洽，
对真实实现零约束力。

真实行为证据在设备端：设置页「投屏日志」里应看到
`M-SEARCH ... ST=ssdp:all → 6 条回复`，以及每 15 分钟一次的 `NOTIFY ssdp:alive × 6`。

用法: python scripts/verify_ssdp.py
"""
from __future__ import annotations

import os
import re
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "..", "UnCast", "DLNA", "SSDPResponder.swift")
SVC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "..", "UnCast", "DLNA", "DLNAService.swift")

# UPnP MediaRenderer:1 必须通告的 target（少一个，对应类型的客户端就看不见）
REQUIRED_TARGETS = [
    "upnp:rootdevice",
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "urn:schemas-upnp-org:service:AVTransport:1",
    "urn:schemas-upnp-org:service:RenderingControl:1",
    "urn:schemas-upnp-org:service:ConnectionManager:1",
]


class Checker:
    def __init__(self):
        self.passed = 0
        self.failed: list[tuple[str, str]] = []

    def check(self, name, cond, detail=""):
        if cond:
            self.passed += 1
            print(f"  ✅ {name}")
        else:
            self.failed.append((name, detail))
            print(f"  ❌ {name}  {detail}")

    def summary(self):
        total = self.passed + len(self.failed)
        print(f"\n结果: {self.passed}/{total} 通过")
        if self.failed:
            print("失败项:")
            for n, d in self.failed:
                print(f"  - {n}  {d}")
        return not self.failed


def strip_comments(src: str) -> str:
    """剥掉 // 注释与 /* */ 块，避免注释里的示例代码被当成真实代码。

    不剥的后果很具体：replyTargets 的注释里就写着 "ST: ssdp:all"，
    拿它去判断「是否回显了 ssdp:all」会永远命中，断言变成永真。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    out = []
    for line in src.splitlines():
        # 只剥行尾 //：字符串里出现的 // (如 http://) 不能误伤
        in_str = False
        cut = len(line)
        i = 0
        while i < len(line):
            c = line[i]
            if c == '"' and (i == 0 or line[i - 1] != "\\"):
                in_str = not in_str
            elif not in_str and line.startswith("//", i):
                cut = i
                break
            i += 1
        out.append(line[:cut])
    return "\n".join(out)


def extract_block(src: str, header_pat: str, open_ch: str = "{", close_ch: str = "}") -> str:
    """从 header_pat 起始位置起，按括号配平截出整段。

    用配平而不是找下一个 '\\n    }'：Swift 里嵌套闭包很多，
    简单的「下一个右括号」会截断在多行数组中间。

    open_ch/close_ch 可切换成 [] —— 常量数组是数组字面量，用大括号配平
    会一个字符都截不到。踩过：searchTargets 抽到 0 条就是这么来的，
    产物是对的、判据是错的。
    """
    m = re.search(header_pat, src)
    if not m:
        return ""
    start = src.find(open_ch, m.end() - 1)
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(src)):
        if src[i] == open_ch:
            depth += 1
        elif src[i] == close_ch:
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    return ""


def main() -> int:
    if not os.path.isfile(SRC):
        print(f"找不到 {SRC}")
        return 2

    raw = open(SRC, encoding="utf-8").read()
    src = strip_comments(raw)
    c = Checker()

    print("=" * 68)
    print("SSDP 发现契约校验（源码结构层）")
    print("=" * 68)

    # ── A. 通告目录完整性 ──
    print("\n[A] 通告 target 目录")
    block = extract_block(src, r"static let searchTargets:\s*\[String\]", "[", "]")
    found = re.findall(r'"(urn:[^"]+|upnp:[^"]+)"', block)
    c.check("searchTargets 可解析", bool(found), f"抽到 {len(found)} 条")
    for t in REQUIRED_TARGETS:
        c.check(f"含 {t}", t in found, f"实际 {found}")
    c.check("无重复 target",
            len(found) == len(set(found)), f"实际 {found}")

    # ── B. ssdp:all 不能原样回显（本次修复的核心） ──
    print("\n[B] ssdp:all 的回复（历史缺陷：原样回显导致客户端丢弃）")
    rt = extract_block(src, r"private func replyTargets\(for st: String\)")
    c.check("replyTargets 存在", bool(rt), "未找到函数")
    if rt:
        # 找出 ssdp:all 分支的 return
        m = re.search(r'if\s+s\s*==\s*"ssdp:all"\s*\{', rt)
        c.check("有 ssdp:all 分支", bool(m), "未找到")
        if m:
            branch = extract_block(rt[m.start():], r'if\s+s\s*==\s*"ssdp:all"')
            # return 可能在同一行（`{ return [st] }`），也可能另起一行。
            # 早先只匹配 `return ...\n`，单行写法抽不到 → 报「分支有 return 未找到」。
            # 那是判据的缺陷：产物是被注入坏的，但报错指向了错误原因。
            ret = re.search(r"return\s+([^\n}]+)", branch)
            ret_expr = ret.group(1).strip() if ret else ""
            c.check("ssdp:all 分支有 return", bool(ret_expr), "未找到")
            # 关键断言：不能直接把请求的 st 返回
            c.check("ssdp:all 不回显 st 本身",
                    ret_expr != "[st]" and "return [st]" not in branch,
                    f"实际 return {ret_expr}")
            # 必须展开成多个具体 target
            c.check("ssdp:all 展开为多个 target",
                    "searchTargets" in ret_expr,
                    f"实际 return {ret_expr}")
            # 必须包含裸 UDN（缺陷 3）
            c.check("ssdp:all 回复含裸 UDN",
                    "uuid:" in ret_expr.lower(),
                    f"实际 return {ret_expr}")

    # ── C. 周期通告（缺陷 2） ──
    print("\n[C] 周期通告（历史缺陷：只在启动时发一次）")
    c.check("存在 startNotifyLoop",
            bool(re.search(r"private func startNotifyLoop\(\)", src)), "未找到")
    start_blk = extract_block(src, r"func start\(\) throws")
    c.check("start() 内启动了通告循环",
            "startNotifyLoop()" in start_blk, "start() 未调用")
    # 必须在 isRunning 置真之后启动
    if start_blk and "startNotifyLoop()" in start_blk:
        i_run = start_blk.find("isRunning = true")
        i_loop = start_blk.find("startNotifyLoop()")
        c.check("通告循环在 isRunning 置真之后启动",
                -1 < i_run < i_loop, f"isRunning@{i_run}, loop@{i_loop}")
    # 循环体必须真的调 announceAlive
    loop_blk = extract_block(src, r"private func startNotifyLoop\(\)")
    c.check("循环体内调 announceAlive",
            "announceAlive()" in loop_blk, "未调用")
    # 循环体里两层 while 都要检查 isRunning：
    # 外层决定「还发不发」，内层决定「stop() 后多快退出」。
    # 只查外层的话，内层去掉 isRunning 后 stop() 最坏要等满 900 秒才退出 ——
    # 那时人已经以为服务停了，端口却还占着。
    while_cond = [ln for ln in loop_blk.splitlines()
                  if ln.strip().startswith("while ")]
    c.check("循环体内有两层 while",
            len(while_cond) >= 2, f"实际 {len(while_cond)} 层")
    c.check("两层 while 都检查 isRunning",
            len([ln for ln in while_cond if "isRunning" in ln]) >= 2,
            f"实际条件: {[ln.strip() for ln in while_cond]}")

    # ── D. 通告周期与 max-age 的关系（缺陷 2 的另一半） ──
    print("\n[D] 通告周期 = max-age 的一半")
    m_age = re.search(r"static let maxAge\s*=\s*(\d+)", src)
    c.check("maxAge 可解析", bool(m_age), "未找到")
    age = int(m_age.group(1)) if m_age else 0
    c.check("maxAge 在合理区间(1800)",
            900 <= age <= 86400, f"实际 {age}")
    # 判据：notifyInterval 的定义必须由 maxAge 推导，不能是独立常量。
    # 正则放宽到「同一行里既有 maxAge 又有 /2」即可 —— 早先写死
    # `maxAge\s*/\s*2` 会因为实际写法是 `Double(maxAge) / 2` 而误报 FAIL。
    ni_line = next((ln for ln in src.splitlines()
                    if "static let notifyInterval" in ln), "")
    c.check("notifyInterval 由 maxAge 推导",
            "maxAge" in ni_line and re.search(r"/\s*2\b", ni_line) is not None,
            f"实际定义: {ni_line.strip() or '<未找到>'}")
    # 硬编码数字的地方要检查是否和 maxAge 一致
    hard_max_age = re.findall(r"CACHE-CONTROL:\s*max-age=(\d+)", src)
    c.check("CACHE-CONTROL 不再硬编码数字",
            not hard_max_age,
            f"仍有硬编码 {hard_max_age}，改 maxAge 时会脱钩")

    # ── E. BOOTID（缺陷 4） ──
    print("\n[E] BOOTID 必须随进程启动变化")
    c.check("BOOTID 不由字面量 1 提供",
            "BOOTID.UPNP.ORG: 1" not in src,
            "仍是硬编码 1，App 重启后客户端用旧缓存")
    c.check("BOOTID 有独立定义",
            bool(re.search(r"static let bootID", src)), "未找到 bootID")
    c.check("BOOTID 持久化递增",
            "UserDefaults" in extract_block(src, r"static let bootID")
            if "static let bootID" in src else False,
            "未持久化，重启后可能重复")

    # ── F. USN 格式 ──
    print("\n[F] USN 格式")
    usn_blk = extract_block(src, r"private func usn\(for target: String\)")
    c.check("usn() 存在", bool(usn_blk), "未找到")
    c.check("裸 UDN 不加 :: 后缀",
            'hasPrefix("uuid:")' in usn_blk or 'hasPrefix("uuid:")' in src,
            "裸 UDN 加后缀会让客户端看到幽灵设备")
    c.check("其它 target 用 :: 连接",
            "::" in usn_blk, "未用 :: 连接")

    # ── G. 单播回复目标地址 ──
    print("\n[G] 回复必须单播到请求方")
    resp_blk = extract_block(src, r"private func respondUnicast")
    c.check("respondUnicast 存在", bool(resp_blk), "未找到")
    c.check("回复发给请求方 IP:端口",
            "port: port" in resp_blk, "未按来源端口发送")
    c.check("M-SEARCH 分支逐条回多目标",
            "for t in targets" in src, "只回了一条")
    c.check("DATE 头存在（UPnP 1.1 必需）",
            "DATE:" in resp_blk, "缺 DATE 头")
    c.check("EXT 头存在（必须为空值）",
            re.search(r'"EXT:"', resp_blk) is not None, "EXT 不为空值")

    # ── H. 与 DLNAService 的接线（不能两处都发通告） ──
    print("\n[H] 通告只由一处负责")
    if os.path.isfile(SVC):
        svc = strip_comments(open(SVC, encoding="utf-8").read())
        c.check("DLNAService 不再重复发启动通告",
                "responder.announceLive()" not in svc
                and "responder.announceAlive()" not in svc,
                "两处都发会重复通告")
    else:
        c.check("DLNAService.swift 可读", False, "文件缺失")

    # ── I. SERVER 串在 SSDP 与 HTTP 两处必须一致 ──
    #
    # 不一致的后果：同一台设备在 SSDP 响应里自报 UPnP/1.1、
    # 在 HTTP 响应头里自报 UPnP/1.0，严格客户端会当成两台不同设备，
    # 于是设备列表里出现两个条目、点哪个都连不上。
    # 踩过：改 SSDP 的 serverString 时忘了 HTTP 那处，一版包里两串并存。
    print("\n[I] SERVER 串两处一致")
    http_src_path = os.path.join(os.path.dirname(SRC), "HTTPServer.swift")
    if os.path.isfile(http_src_path):
        http_src = strip_comments(open(http_src_path, encoding="utf-8").read())
        c.check("HTTP 的 Server 头复用 SSDP 的同一常量",
                "SSDPResponder.serverString" in http_src,
                "HTTP 侧另有字面量，改一处会不一致")
        # 反向：HTTP 里不应再有独立的 UPnP 版本字面量
        leftover = re.findall(r'"UnCast/[\d.]+ UPnP/[\d.]+[^"]*"', http_src)
        c.check("HTTP 侧无残留的独立 SERVER 字面量",
                not leftover, f"残留 {leftover}")
    else:
        c.check("HTTPServer.swift 可读", False, "文件缺失")

    print()
    print("=" * 68)
    ok = c.summary()
    print("运行时证据（设备端）: 设置页投屏日志应出现")
    print("  M-SEARCH from <手机IP> ST=ssdp:all → 6 条回复")
    print("  已发送 NOTIFY ssdp:alive × 6   （每 15 分钟一次）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())