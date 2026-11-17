#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_tipa.py 的本地回归测试（不需要 Xcode / 真机）。

## 为什么需要这个文件

CI run #33 的失败属于「修好 A 暴露 B」：

  Gate 2 通过（图标真的编进包）→ 包里多了 Assets.car
  → check_tipa.py 的「可执行文件候选数必须等于 1」当场失败
  → 它还把 Assets.car 当 Mach-O 解析，magic 报 0x534d4f42

0x534d4f42 反序就是 "CARC" —— 图标归档的魔数。所以这不是产物坏了，
是**判据用文件名黑名单猜可执行文件**，包里多任何文件都会让候选漂移。

本文件把这一课固化成用例：**同一份合法产物，加一个 Assets.car 必须仍然绿。**
这条断言如果哪天被改回黑名单法，这里立刻红。

## 合成的 Mach-O 够真吗

够，因为 check_tipa.py 只读固定偏移的几个字段：
  mach_header_64(32B) → LC_BUILD_VERSION → LC_CODE_SIGNATURE → SuperBlob → plist
本文件按 Mach-O 规范逐字节拼出来，字段含义与真实编译产物一致。
不追求能执行 —— 门禁也不执行它，只读字节。

用法:
    python3 scripts/selftest_tipa.py [真实产物路径]
"""
from __future__ import annotations

import io
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
GATE = os.path.join(HERE, "check_tipa.py")
PY = sys.executable or "python3"

MH_MAGIC_64 = 0xFEEDFACF
CPU_TYPE_ARM64 = 0x0100000C
LC_BUILD_VERSION = 0x32
LC_CODE_SIGNATURE = 0x1D
PLATFORM_TVOS = 3
PLATFORM_IOS = 2

CSMAGIC_EMBEDDED_SIGNATURE = 0xFADE0CC0
CSMAGIC_EMBEDDED_ENTITLEMENTS = 0xFADE7171
CSSLOT_ENTITLEMENTS = 5

GOOD_ENTS = {
    "com.apple.developer.networking.multicast": True,
    "com.apple.security.network.server": True,
}


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


# ------------------------------------------------------------ 合成 Mach-O

def build_macho(platform: int = PLATFORM_TVOS,
                cryptid: int | None = None,
                ents: dict | None = None,
                filetype: int = 2,
                cputype: int = CPU_TYPE_ARM64) -> bytes:
    """按 Mach-O 规范拼一个 check_tipa.py 能读的最小可执行文件。

    cryptid=None 表示不带 LC_ENCRYPTION_INFO（== 未加密，合法）；
    传 1 表示加密包（装上会崩，门禁必须拒）。
    ents=None 表示不带 LC_CODE_SIGNATURE 的 entitlements blob。
    """
    LC_ENCRYPTION_INFO_64 = 0x2C

    # ---- 先定各 load command 的字节
    cmds = []

    # LC_BUILD_VERSION: cmd, cmdsize, platform, minos, sdk, ntools = 24 字节
    cmds.append(struct.pack("<IIIIII", LC_BUILD_VERSION, 24,
                            platform, 0x000F0000, 0x00120000, 0))

    enc_cmd = b""
    if cryptid is not None:
        # LC_ENCRYPTION_INFO_64: cmd, cmdsize, cryptoff, cryptsize,
        #                        cryptid, pad  → cryptid 在 +16
        enc_cmd = struct.pack("<IIIIII", LC_ENCRYPTION_INFO_64, 24,
                              0x1000, 0x1000, cryptid, 0)
        cmds.append(enc_cmd)

    # LC_CODE_SIGNATURE 的 dataoff/dataSize 要等算完偏移才知道，先占位
    CS_CMDSIZE = 16
    cs_index = len(cmds)
    cmds.append(b"\x00" * CS_CMDSIZE)

    # ---- 排布布局
    header_size = 32
    sizeofcmds = sum(len(c) for c in cmds)
    body_start = header_size + sizeofcmds

    sig_blob = b""
    if ents is not None:
        sig_blob = build_superblob(ents)

    # 数据段从 16 字节对齐处开始
    pad = (-body_start) % 16
    cs_off = body_start + pad
    cs_size = len(sig_blob)
    data = bytearray(cs_off + cs_size)

    # ---- 头
    struct.pack_into("<IIIIIIII", data, 0,
                     MH_MAGIC_64, cputype, 0, filetype,
                     len(cmds), sizeofcmds, 0, 0)

    # ---- load commands
    off = header_size
    for i, c in enumerate(cmds):
        if i == cs_index:
            struct.pack_into("<IIII", data, off, LC_CODE_SIGNATURE,
                             CS_CMDSIZE, cs_off, cs_size)
        else:
            data[off:off + len(c)] = c
        off += len(c)

    # ---- 签名区
    if sig_blob:
        data[cs_off:cs_off + len(sig_blob)] = sig_blob

    return bytes(data)


def build_superblob(ents: dict) -> bytes:
    """SuperBlob(magic,length,count) + 索引项 + Entitlements blob。"""
    payload = plistlib.dumps(ents)
    # blob = magic(4) + length(4) + payload；length 是 blob 总长（含头）
    blob = struct.pack(">II", CSMAGIC_EMBEDDED_ENTITLEMENTS, 8 + len(payload)) + payload
    index_size = 12 + 8          # 头 + 1 个索引项
    total = index_size + len(blob)
    head = struct.pack(">III", CSMAGIC_EMBEDDED_SIGNATURE, total, 1)
    idx = struct.pack(">II", CSSLOT_ENTITLEMENTS, index_size)
    return head + idx + blob


# ------------------------------------------------------------ 造 tipa

def make_tipa(path: str,
              platform: int = PLATFORM_TVOS,
              cryptid: int | None = None,
              ents: dict | None = GOOD_ENTS,
              include_car: bool = False,
              exe_name_in_plist: str = "UnCast",
              exe_file_name: str = "UnCast",
              extra_files: dict | None = None):
    """写一个合成 tipa。默认参数下是「合格产物」。"""
    binary = build_macho(platform=platform, cryptid=cryptid, ents=ents)

    info = {
        "CFBundleIdentifier": "com.uncast.tv",
        "CFBundleName": "UnCast",
        "CFBundleExecutable": exe_name_in_plist,
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleSupportedPlatforms": ["AppleTVOS"],
        "DTPlatformName": "appletvos",
        "UIDeviceFamily": [3],
        "CFBundleIcons": {"CFBundlePrimaryIcon": "App Icon"},
        "TVTopShelfImage": {
            "TVTopShelfPrimaryImage": "Top Shelf Image",
            "TVTopShelfPrimaryImageWide": "Top Shelf Image Wide",
        },
    }

    entries = {
        "Payload/UnCast.app/Info.plist": plistlib.dumps(info),
        "Payload/UnCast.app/PkgInfo": b"APPL????",
        f"Payload/UnCast.app/{exe_file_name}": binary,
    }
    if include_car:
        # 真实 car 开头是 "BOSM"（即 "CARC" 按小端读回来的样子）
        car = b"BOSM" + b"App Icon" + b"Top Shelf Image"
        entries["Payload/UnCast.app/Assets.car"] = car + b"\x00" * (2048 - len(car))
    if extra_files:
        entries.update(extra_files)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, blob in entries.items():
            z.writestr(name, blob)


def run_gate(path: str):
    p = subprocess.run([PY, GATE, path], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, p.stdout, p.stderr


def crashed(rc, err):
    return "Traceback" in err or rc not in (0, 1, 2)


def main() -> int:
    c = C()
    tmp = tempfile.mkdtemp(prefix="uncst_tipa_")
    try:
        # ── A. 基线：合格产物必须绿 ──
        print("\n== A. 基线（无 Assets.car）==")
        p = os.path.join(tmp, "base.tipa")
        make_tipa(p)
        rc, out, err = run_gate(p)
        c.check(not crashed(rc, err), "A1 不崩溃", err.strip()[:300])
        c.check(rc == 0, "A2 合格产物通过", f"rc={rc}")
        c.check("10/10" in out or "通过" in out, "A3 有完整判定输出")

        # ── B. run #33 回归：多了 Assets.car 必须仍然绿 ──
        print("\n== B. 回归 — 包里有 Assets.car（run #33 的假 FAIL）==")
        p = os.path.join(tmp, "withcar.tipa")
        make_tipa(p, include_car=True)
        rc, out, err = run_gate(p)
        c.check(not crashed(rc, err), "B1 不崩溃", err.strip()[:300])
        c.check(rc == 0, "B2 有 Assets.car 仍然通过（黑名单法会在这里红）",
                f"rc={rc}")
        c.check("按 CFBundleExecutable 定位" in out,
                "B3 可执行文件确实按 CFBundleExecutable 定位")
        c.check("0x534d4f42" not in out,
                "B4 没有把 Assets.car 当 Mach-O 解析")

        # ── C. 反向：缺 multicast entitlement ──
        print("\n== C. 反向 — 缺 multicast entitlement ==")
        p = os.path.join(tmp, "noent.tipa")
        make_tipa(p, ents={"com.apple.security.network.server": True})
        rc, out, err = run_gate(p)
        c.check(rc == 1, "C1 判不合格", f"rc={rc}")
        c.check("multicast" in out, "C2 失败项点名 multicast")

        # ── D. 反向：完全没有签名 blob ──
        print("\n== D. 反向 — 没有 entitlements blob ==")
        p = os.path.join(tmp, "nosig.tipa")
        make_tipa(p, ents=None)
        rc, out, err = run_gate(p)
        c.check(rc == 1, "D1 判不合格", f"rc={rc}")
        c.check("能读到 entitlements" in out, "D2 点出读不到 entitlements")

        # ── E. 反向：加密包 ──
        print("\n== E. 反向 — cryptid=1（FairPlay 加密）==")
        p = os.path.join(tmp, "enc.tipa")
        make_tipa(p, cryptid=1)
        rc, out, err = run_gate(p)
        c.check(rc == 1, "E1 判不合格", f"rc={rc}")
        c.check("FairPlay" in out or "cryptid" in out, "E2 点出加密问题")

        # ── F. 反向：iOS 平台 ──
        print("\n== F. 反向 — platform=2（iOS 而不是 tvOS）==")
        p = os.path.join(tmp, "ios.tipa")
        make_tipa(p, platform=PLATFORM_IOS)
        rc, out, err = run_gate(p)
        c.check(rc == 1, "F1 判不合格", f"rc={rc}")
        c.check("平台" in out, "F2 点出平台不对")
        c.check("3 才是 tvOS" in out, "F3 提示里说明了 3 才是 tvOS")

        # ── G. 反向：CFBundleExecutable 指向不存在的文件 ──
        print("\n== G. 反向 — CFBundleExecutable 与包内文件不一致 ==")
        p = os.path.join(tmp, "mismatch.tipa")
        make_tipa(p, exe_name_in_plist="UnCastRenamed")
        rc, out, err = run_gate(p)
        c.check(rc == 1, "G1 判不合格", f"rc={rc}")
        c.check("含可执行文件" in out, "G2 点出找不到可执行文件")

        # ── H. 反向：少了 UIDeviceFamily=3 ──
        print("\n== H. 反向 — UIDeviceFamily 不含 3 ==")
        p = os.path.join(tmp, "family.tipa")
        binary = build_macho()
        info = {
            "CFBundleIdentifier": "com.uncast.tv",
            "CFBundleExecutable": "UnCast",
            "CFBundleSupportedPlatforms": ["AppleTVOS"],
            "DTPlatformName": "appletvos",
            "UIDeviceFamily": [1, 2],
        }
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("Payload/UnCast.app/Info.plist", plistlib.dumps(info))
            z.writestr("Payload/UnCast.app/UnCast", binary)
        rc, out, err = run_gate(p)
        c.check(rc == 1, "H1 判不合格", f"rc={rc}")
        c.check("UIDeviceFamily" in out, "H2 点出 UIDeviceFamily")

        return c.report("check_tipa.py 回归测试")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())