#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UnCast 产物门禁（CI 用）。

为什么需要它：CI 里 ``BUILD SUCCEEDED`` 和 ``zip`` 成功**都不能**证明包能装在
Apple TV 上。真正决定成败的是二进制字节里的三件事，任何一项错了设备上都会：

  1. 平台不是 tvOS      → 安装器直接拒绝
  2. cryptid != 0       → FairPlay 加密包，装上必崩
  3. 缺 multicast 权限  → IP_ADD_MEMBERSHIP 返回 EPERM，手机搜不到设备

本脚本只读原始字节判定，不信构建日志的任何声明。
任一检查失败 → 非 0 退出码 → CI 变红。
"""
import plistlib
import struct
import sys
import zipfile

# ---- 常量 ----
MH_MAGIC_64 = 0xFEEDFACF
CPU_TYPE_ARM64 = 0x0100000C
LC_BUILD_VERSION = 0x32
LC_VERSION_MIN_TVOS = 0x2F
LC_ENCRYPTION_INFO = 0x21
LC_ENCRYPTION_INFO_64 = 0x2C
LC_CODE_SIGNATURE = 0x1D

CSMAGIC_EMBEDDED_SIGNATURE = 0xFADE0CC0
CSMAGIC_EMBEDDED_ENTITLEMENTS = 0xFADE7171
CSSLOT_ENTITLEMENTS = 5

PLATFORM_TVOS = 3

REQUIRED_ENTITLEMENTS = [
    "com.apple.developer.networking.multicast",
    "com.apple.security.network.server",
]


class Gate:
    def __init__(self):
        self.failures = []
        self.checks = 0

    def check(self, name, ok, detail=""):
        self.checks += 1
        if ok:
            print(f"  [PASS] {name}")
        else:
            print(f"  [FAIL] {name}" + (f"  -> {detail}" if detail else ""))
            self.failures.append(name)

    def result(self):
        print()
        print(f"门禁结果: {self.checks - len(self.failures)}/{self.checks} 通过")
        if self.failures:
            print("失败项:")
            for f in self.failures:
                print(f"  - {f}")
            return False
        return True


def read_entitlements(data):
    """从 LC_CODE_SIGNATURE 的 SuperBlob 里取出 entitlements 字典。"""
    if len(data) < 32:
        return None
    magic_le = struct.unpack("<I", data[:4])[0]
    endian = "<" if magic_le == MH_MAGIC_64 else ">"
    if magic_le != MH_MAGIC_64:
        magic_be = struct.unpack(">I", data[:4])[0]
        if magic_be == MH_MAGIC_64:
            endian = ">"
        else:
            return None

    ncmds = struct.unpack(endian + "I", data[16:20])[0]
    off = 32
    cs_off = cs_size = None
    for _ in range(ncmds):
        if off + 8 > len(data):
            break
        cmd, cmdsize = struct.unpack(endian + "II", data[off:off + 8])
        if cmdsize < 8:
            break
        if cmd == LC_CODE_SIGNATURE:
            cs_off, cs_size = struct.unpack(endian + "II", data[off + 8:off + 16])
            break
        off += cmdsize

    if cs_off is None:
        return None

    # SuperBlob 内部一律 big-endian
    if struct.unpack(">I", data[cs_off:cs_off + 4])[0] != CSMAGIC_EMBEDDED_SIGNATURE:
        return None
    count = struct.unpack(">I", data[cs_off + 8:cs_off + 12])[0]
    for i in range(count):
        e = cs_off + 12 + i * 8
        btype = struct.unpack(">I", data[e:e + 4])[0]
        boff = struct.unpack(">I", data[e + 4:e + 8])[0]
        if btype == CSSLOT_ENTITLEMENTS:
            p = cs_off + boff
            m = struct.unpack(">I", data[p:p + 4])[0]
            ln = struct.unpack(">I", data[p + 4:p + 8])[0]
            if m != CSMAGIC_EMBEDDED_ENTITLEMENTS:
                return None
            try:
                return plistlib.loads(data[p + 8:p + ln])
            except Exception:
                return None
    return None


def analyse_macho(data, gate, label):
    """逐项判定 Mach-O 关键属性。"""
    if len(data) < 32:
        gate.check(f"{label}: 是 Mach-O", False, "数据过短")
        return

    magic_le = struct.unpack("<I", data[:4])[0]
    gate.check(f"{label}: MH_MAGIC_64", magic_le == MH_MAGIC_64,
               f"magic=0x{magic_le:08x}")

    if magic_le != MH_MAGIC_64:
        return

    endian = "<"
    cputype, cpusub, filetype, ncmds, sizeofcmds, flags = struct.unpack(
        endian + "IIIIII", data[4:28])

    gate.check(f"{label}: 架构 arm64", cputype == CPU_TYPE_ARM64,
               f"cputype=0x{cputype:08x}")
    gate.check(f"{label}: filetype=MH_EXECUTE", filetype == 2, f"filetype={filetype}")

    off = 32
    platform = None
    cryptid = None
    has_codesig = False
    for _ in range(ncmds):
        if off + 8 > len(data):
            break
        cmd, cmdsize = struct.unpack(endian + "II", data[off:off + 8])
        if cmdsize < 8:
            break
        if cmd == LC_BUILD_VERSION:
            plat = struct.unpack(endian + "I", data[off + 8:off + 12])[0]
            platform = plat
        elif cmd == LC_VERSION_MIN_TVOS:
            platform = PLATFORM_TVOS
        elif cmd in (LC_ENCRYPTION_INFO, LC_ENCRYPTION_INFO_64):
            cryptid = struct.unpack(endian + "I", data[off + 16:off + 20])[0]
        elif cmd == LC_CODE_SIGNATURE:
            has_codesig = True
        off += cmdsize

    gate.check(f"{label}: 平台是 tvOS", platform == PLATFORM_TVOS,
               f"platform={platform}（3 才是 tvOS）")
    # cryptid None 表示没有加密命令 = 未加密，合法
    gate.check(f"{label}: 未 FairPlay 加密", cryptid in (None, 0),
               f"cryptid={cryptid}（非 0 装上会崩）")
    gate.check(f"{label}: 已签名（有 LC_CODE_SIGNATURE）", has_codesig)


def main(tipa_path):
    print("=" * 60)
    print("UnCast 产物门禁")
    print("=" * 60)
    gate = Gate()

    # ---- 1. tipa 结构 ----
    print("\n[1] tipa 包结构")
    try:
        z = zipfile.ZipFile(tipa_path)
    except Exception as e:
        print(f"  [FAIL] 无法打开 tipa: {e}")
        return 1

    names = z.namelist()
    gate.check("含 Payload/UnCast.app/", any("Payload/UnCast.app/" in n for n in names))
    gate.check("含 Info.plist", any(n.endswith("UnCast.app/Info.plist") for n in names))

    # 只在「文件」里找可执行文件：必须排除以 / 结尾的目录项，
    # 否则 Payload/UnCast.app/ 本身也会被算作候选（曾经的假 FAIL 来源）。
    file_names = [n for n in names if not n.endswith("/")]
    exe_candidates = [
        n for n in file_names
        if n.count("/") == 2 and n.startswith("Payload/UnCast.app/")
        and not n.endswith(".plist") and not n.endswith("PkgInfo")
        and not n.endswith(".nib") and not n.endswith(".png")
    ]
    gate.check("含可执行文件", len(exe_candidates) == 1, f"候选={exe_candidates}")

    if not exe_candidates:
        return 0 if gate.result() else 1

    exe_name = exe_candidates[0]

    # ---- 2. Info.plist ----
    print("\n[2] Info.plist")
    try:
        plist_name = [n for n in names if n.endswith("UnCast.app/Info.plist")][0]
        info = plistlib.loads(z.read(plist_name))
    except Exception as e:
        print(f"  [FAIL] Info.plist 解析失败: {e}")
        return 1

    gate.check("UIDeviceFamily 含 3（Apple TV）",
               3 in (info.get("UIDeviceFamily") or []),
               f"实际 {info.get('UIDeviceFamily')}")
    gate.check("CFBundleSupportedPlatforms 含 AppleTVOS",
               "AppleTVOS" in (info.get("CFBundleSupportedPlatforms") or []),
               f"实际 {info.get('CFBundleSupportedPlatforms')}")
    gate.check("DTPlatformName == appletvos",
               info.get("DTPlatformName") == "appletvos",
               f"实际 {info.get('DTPlatformName')}")
    gate.check("CFBundleIdentifier 非空",
               bool(info.get("CFBundleIdentifier")),
               f"实际 {info.get('CFBundleIdentifier')}")
    gate.check("CFBundleExecutable 与二进制名一致",
               info.get("CFBundleExecutable") == exe_name.rsplit("/", 1)[-1],
               f"plist={info.get('CFBundleExecutable')} 文件={exe_name}")

    # ---- 3. Mach-O ----
    print("\n[3] Mach-O 属性")
    data = z.read(exe_name)
    analyse_macho(data, gate, "binary")

    # ---- 4. entitlements ----
    print("\n[4] 嵌入的 entitlements（组播必需）")
    ents = read_entitlements(data)
    if ents is None:
        gate.check("能读到 entitlements", False,
                   "签名里没有 entitlements blob → SSDP 组播会被沙盒拒绝")
    else:
        print(f"  entitlements keys: {sorted(ents.keys())}")
        for key in REQUIRED_ENTITLEMENTS:
            gate.check(f"{key} = true", ents.get(key) is True, f"实际 {ents.get(key)!r}")

    ok = gate.result()
    print()
    print("结论:", "产物可用于 TrollStore 安装" if ok else "产物不合格，禁止发布")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法: python check_tipa.py <path/to/UnCast.tipa>")
    sys.exit(main(sys.argv[1]))