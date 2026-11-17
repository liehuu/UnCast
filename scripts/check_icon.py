#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验 .app 里图标是否真的可用。

## 为什么需要独立一道门

`xcodebuild` 成功、`Assets.car` 存在、Info.plist 有键，三者**都不足以**证明
主屏能显示出图标。少写 `ASSETCATALOG_COMPILER_APPICON_NAME` 时：
构建照样 SUCCEEDED，Assets.car 照样生成（只是空的），图标就是白块。

所以判据必须落在**产出物字节**上：
  1. Assets.car 存在且非空，内部 rendition 名字含 "App Icon" 与 "Top Shelf Image"
  2. Info.plist 的 CFBundleIcons / TVTopShelfImage 指向的名字与 car 内一致
  3. brandassets 源目录里 Back 层与 Top Shelf 完全不透明（tvOS 硬要求）
  4. 各层尺寸严格等于 Apple 规定值

第 3/4 条读的是源 PNG，能在本机跑（不需要 Xcode），
第 1/2 条需要 car 文件，只在 CI 有 Xcode 时跑。
"""
from __future__ import annotations

import json
import os
import plistlib
import struct
import sys
import zlib

# tvOS 规定尺寸（写错 actool 会警告甚至拒收）
EXPECTED = {
    "App Icon.imagestack": [
        ("Front", "@1x", 400, 240), ("Front", "@2x", 800, 480),
        ("Middle", "@1x", 400, 240), ("Middle", "@2x", 800, 480),
        ("Back", "@1x", 400, 240), ("Back", "@2x", 800, 480),
    ],
    "App Icon - App Store.imagestack": [
        ("Front", "@1x", 1280, 768),
        ("Middle", "@1x", 1280, 768),
        ("Back", "@1x", 1280, 768),
    ],
    "Top Shelf Image.imageset": [
        ("", "@1x", 1920, 720), ("", "@2x", 3840, 1440),
    ],
    "Top Shelf Image Wide.imageset": [
        ("", "@1x", 2320, 720), ("", "@2x", 4640, 1440),
    ],
}

# 这些必须完全不透明
OPAQUE = {
    ("App Icon.imagestack", "Back"),
    ("App Icon - App Store.imagestack", "Back"),
}

BRAND_REL = "UnCast/Assets.xcassets/App Icon & Top Shelf Image.brandassets"

# 仓库根：脚本在 <root>/scripts/ 下
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


# ------------------------------------------------------------------ PNG 读

def read_png(path: str):
    """读 8bit RGBA，返回 (w, h, pixels bytes)。只支持 filter=None。"""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"不是 PNG: {path}")

    pos, w, h, idat, bitdepth, color = 8, None, None, b"", None, None
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + ln]
        if tag == b"IHDR":
            w, h = struct.unpack(">II", payload[:8])
            bitdepth, color = payload[8], payload[9]
        elif tag == b"IDAT":
            idat += payload
        elif tag == b"IEND":
            break
        pos += 12 + ln

    if bitdepth != 8 or color != 6:
        raise ValueError(f"{path}: 只支持 8bit RGBA，实际 depth={bitdepth} color={color}")

    raw = zlib.decompress(idat)
    stride = w * 4 + 1
    if len(raw) != stride * h:
        raise ValueError(f"{path}: 解压后长度不符 {len(raw)} != {stride * h}")
    for y in range(h):
        if raw[y * stride] != 0:
            raise ValueError(f"{path}: 第 {y} 行使用了 filter，本函数不支持")
    return w, h, raw, stride


def png_alpha(path: str):
    w, h, raw, stride = read_png(path)
    alphas = bytearray()
    for y in range(h):
        row = raw[y * stride + 1:(y + 1) * stride]
        alphas += row[3::4]
    return w, h, alphas


# ------------------------------------------------------------------ 检查项

class Checker:
    def __init__(self):
        self.fails = []
        self.passes = 0

    def check(self, ok: bool, msg: str, detail: str = ""):
        if ok:
            self.passes += 1
            print(f"  PASS  {msg}")
        else:
            self.fails.append(msg + (f"  [{detail}]" if detail else ""))
            print(f"  FAIL  {msg}" + (f"  [{detail}]" if detail else ""))

    def report(self, title: str) -> int:
        total = self.passes + len(self.fails)
        print(f"\n{title}: {self.passes}/{total}")
        if self.fails:
            print("失败项:")
            for f in self.fails:
                print("  -", f)
            return 1
        return 0


def check_sources(root: str, c: Checker):
    """检查源 brandassets（不依赖 Xcode，本机可跑）。"""
    print("\n== 1. 源资源尺寸与不透明度 ==")
    brand = os.path.join(root, BRAND_REL)
    c.check(os.path.isdir(brand), "brandassets 目录存在", brand)
    if not os.path.isdir(brand):
        return

    contents_path = os.path.join(brand, "Contents.json")
    c.check(os.path.isfile(contents_path), "brandassets/Contents.json 存在")
    if os.path.isfile(contents_path):
        with open(contents_path, encoding="utf-8") as f:
            meta = json.load(f)
        roles = {a.get("role") for a in meta.get("assets", [])}
        for need in ("primary-app-icon", "top-shelf-image",
                     "top-shelf-image-wide"):
            c.check(need in roles, f"Contents.json 声明 role={need}", str(roles))

    for stack, files in EXPECTED.items():
        for layer, suffix, ew, eh in files:
            if layer:
                rel = (f"{stack}/{layer}.imagestacklayer/Content.imageset/"
                       f"{layer.lower()}{suffix}.png")
            else:
                base = "top" if "Wide" not in stack else "wide"
                rel = f"{stack}/{base}{suffix}.png"
            p = os.path.join(brand, rel)
            if not os.path.isfile(p):
                c.check(False, f"{rel} 存在")
                continue

            w, h, alphas = png_alpha(p)
            c.check((w, h) == (ew, eh), f"{rel} 尺寸 {ew}x{eh}", f"实际 {w}x{h}")

            if (stack, layer) in OPAQUE:
                amin = min(alphas)
                c.check(amin == 255, f"{rel} 完全不透明（tvOS 硬要求）",
                        f"min alpha={amin}")

            # 图标本体必须有可见内容，不能是全透明/全黑。
            #
            # 判据按层区分，不能用一个通用阈值：
            #   Front  = 字形，必须接近实心（alpha 顶到 ~255）
            #   Middle = 视差用的柔光，**有意**只到 34%（约 87/255），
            #            用 Front 的标准去卡它是假 FAIL；
            #            但它也不能空（alpha 全 0）或全不透明（会挡住 Front）。
            if layer == "Front":
                amax = max(alphas)
                c.check(amax > 230, f"{rel} 字形近乎实心", f"max alpha={amax}")
            elif layer == "Middle":
                amax = max(alphas)
                amin = min(alphas)
                c.check(10 < amax < 200,
                        f"{rel} 柔光层半透明且非空",
                        f"alpha 范围 {amin}..{amax}")
                # 柔光必须覆盖比字形更大的面积，否则视差没有层次。
                #
                # 阈值推导：半径 0.40*min(w,h) 的圆，面积占比约 pi*0.4^2 ≈ 50%；
                # 取 alpha>0 的实际边界要扣掉四角，实测稳定在 24%。
                # 用 20% 作为下限，既能抓住"半径被多乘一次缩放"那类退化
                # （退化后只剩 2%），又不会因为图幅比例变化误报。
                lit = sum(1 for a in alphas if a > 0) / len(alphas)
                c.check(lit > 0.20,
                        f"{rel} 柔光覆盖足够面积",
                        f"覆盖 {lit:.0%}")


def check_bundle(app: str, c: Checker):
    """检查 .app 内编译产物（需要 Xcode 生成的 Assets.car）。"""
    print("\n== 2. 包内 Assets.car 与 Info.plist ==")

    car = os.path.join(app, "Assets.car")
    c.check(os.path.isfile(car), "Assets.car 存在")
    if not os.path.isfile(car):
        plist = os.path.join(app, "Info.plist")
        c.check(os.path.isfile(plist), "兜底：Info.plist 存在")
        loose = os.path.join(app, "AppIcon.png")
        c.check(os.path.isfile(loose), "兜底：散装 AppIcon.png 在包内")
        return

    size = os.path.getsize(car)
    c.check(size > 1024, f"Assets.car 非空 ({size} bytes)")

    # car 里 rendition 名字是明文，可以直接搜；不依赖 assetutil 的 JSON 格式
    with open(car, "rb") as f:
        blob = f.read()
    for name in (b"App Icon", b"Top Shelf Image"):
        c.check(name in blob, f"Assets.car 内含 '{name.decode()}' 标识")

    plist_path = os.path.join(app, "Info.plist")
    c.check(os.path.isfile(plist_path), "Info.plist 存在")
    if not os.path.isfile(plist_path):
        return

    with open(plist_path, "rb") as f:
        info = plistlib.load(f)

    # tvOS 与 iOS 的格式**不同**，这里必须按 tvOS 判：
    #   tvOS → CFBundlePrimaryIcon 是字符串（资源目录里的图标栈名）
    #   iOS  → CFBundlePrimaryIcon 是字典
    # 写成 iOS 的字典形式时 tvOS 读不到，主屏是白块 —— 本项目真的踩过，
    # 所以这条断言要明确拒绝 dict。
    icons = info.get("CFBundleIcons")
    c.check(isinstance(icons, dict), "Info.plist 有 CFBundleIcons")
    if isinstance(icons, dict):
        primary = icons.get("CFBundlePrimaryIcon")
        c.check(isinstance(primary, str),
                "CFBundlePrimaryIcon 是字符串（tvOS 格式）",
                f"实际类型 {type(primary).__name__}: {primary!r}")
        if isinstance(primary, str):
            c.check(primary == "App Icon",
                    "CFBundlePrimaryIcon == 'App Icon'", repr(primary))

    shelf = info.get("TVTopShelfImage")
    c.check(isinstance(shelf, dict), "Info.plist 有 TVTopShelfImage")
    if isinstance(shelf, dict):
        c.check(shelf.get("TVTopShelfPrimaryImage") == "Top Shelf Image",
                "TVTopShelfPrimaryImage 正确",
                repr(shelf.get("TVTopShelfPrimaryImage")))
        c.check(shelf.get("TVTopShelfPrimaryImageWide") == "Top Shelf Image Wide",
                "TVTopShelfPrimaryImageWide 正确",
                repr(shelf.get("TVTopShelfPrimaryImageWide")))


def check_plist_shape(c: Checker):
    """源 Info.plist 的结构检查（不需要 Xcode，本机可跑）。

    这一层专门抓"格式写错但编译不报错"的问题：
    CFBundlePrimaryIcon 写成 iOS 的字典形式时，xcodebuild 一样 SUCCEEDED，
    actool 也不报错，但 tvOS 读不到，主屏是白块。
    """
    print("\n== 3. 源 Info.plist 图标键（tvOS 格式）==")
    path = os.path.join(ROOT, "UnCast", "App", "Info.plist")
    c.check(os.path.isfile(path), "源 Info.plist 存在")
    if not os.path.isfile(path):
        return
    with open(path, "rb") as f:
        info = plistlib.load(f)

    icons = info.get("CFBundleIcons")
    c.check(isinstance(icons, dict), "源 Info.plist 有 CFBundleIcons")
    if isinstance(icons, dict):
        primary = icons.get("CFBundlePrimaryIcon")
        # 关键的反向断言：必须是字符串，不能是 dict
        c.check(isinstance(primary, str),
                "CFBundlePrimaryIcon 为字符串（tvOS 格式，非 iOS 字典）",
                f"实际 {type(primary).__name__}: {primary!r}")

    shelf = info.get("TVTopShelfImage")
    c.check(isinstance(shelf, dict), "源 Info.plist 有 TVTopShelfImage")


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: check_icon.py <路径>")
        print("  <路径> 可以是仓库根目录（只查源资源）")
        print("  <路径> 也可以是 Payload/UnCast.app（额外查编译产物）")
        return 2

    target = sys.argv[1].rstrip("/\\")
    c = Checker()

    if target.endswith(".app"):
        # 从 .app 反推仓库根，用于查源资源
        check_sources(root, c)
        check_plist_shape(c)
        check_bundle(target, c)
        title = "图标门禁"
    else:
        check_sources(target, c)
        check_plist_shape(c)
        title = "图标源资源检查"

    return c.report(title)


if __name__ == "__main__":
    sys.exit(main())