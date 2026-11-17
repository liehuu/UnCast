#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 tvOS App Icon / Top Shelf 资源（零第三方依赖，输出逐字节可复现）。

为什么手写 PNG 编码器而不是用 Pillow：
1. 本机与 CI 都没有 Pillow，多一个依赖就多一个构建失败点；
2. 图标必须**逐字节可复现**——同样输入必须产出同样 sha256，
   否则每次 CI 出来的包 hash 都不同，Release 校验就失去意义。

绘制方式：有向距离场（SDF）在目标分辨率直接算覆盖率，
抗锯齿来自 clip(0.5 - sdf, 0, 1)，不超采样、不引入模糊。

## tvOS brandassets 的正确结构（写错的典型后果：图标变白块）

    Assets.xcassets/
      App Icon & Top Shelf Image.brandassets/       ← ASSETCATALOG_COMPILER_APPICON_NAME
        Contents.json
        App Icon.imagestack/                        ← 主屏图标 400x240 @1x / 800x480 @2x
          Front.imagestacklayer/Content.imageset/{front@1x,front@2x}.png
          Middle.imagestacklayer/Content.imageset/{middle@1x,middle@2x}.png
          Back.imagestacklayer/Content.imageset/{back@1x,back@2x}.png   ← 必须不透明
        App Icon - App Store.imagestack/            ← 商店图标 1280x768（同样三层）
          Front/Middle/Back.imagestacklayer/Content.imageset/front@1x.png
        Top Shelf Image.imageset/                   ← 1920x720 @1x / 3840x1440 @2x
        Top Shelf Image Wide.imageset/              ← 2320x720 @1x / 4640x1440 @2x

Back 层与 Top Shelf 必须完全不透明，否则 tvOS 会拒收。
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import zlib

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "UnCast", "Assets.xcassets")
BRAND_NAME = "App Icon & Top Shelf Image"
BRAND = os.path.join(ASSETS, BRAND_NAME + ".brandassets")

# 设计基准画幅：所有几何按 min(w/1280, h/768) 等比缩放
BASE_W, BASE_H = 1280, 768

BG_TOP = (0.024, 0.035, 0.075)     # #060912
BG_BOT = (0.075, 0.110, 0.255)     # #131C41
ACCENT = (0.145, 0.435, 1.000)     # #256FF F
GLYPH_TOP = (1.000, 1.000, 1.000)
GLYPH_BOT = (0.839, 0.906, 1.000)  # #D6E7FF


# ---------------------------------------------------------------- PNG 编码

def write_png(path: str, rgba: np.ndarray) -> str:
    """HxWx4 uint8 → PNG（8bit 真彩+alpha，filter=None）。返回 sha256。"""
    assert rgba.dtype == np.uint8 and rgba.ndim == 3 and rgba.shape[2] == 4
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)
    return hashlib.sha256(png).hexdigest()


# ---------------------------------------------------------------- SDF 原语

def _grid(w: int, h: int):
    xs = np.arange(w, dtype=np.float32) + 0.5
    ys = np.arange(h, dtype=np.float32) + 0.5
    return np.meshgrid(xs, ys)


def rounded_rect_sdf(px, py, cx, cy, hw, hh, r):
    """圆角矩形有向距离：内部为负，外部为正。"""
    qx = np.abs(px - cx) - (hw - r)
    qy = np.abs(py - cy) - (hh - r)
    ax = np.maximum(qx, 0.0)
    ay = np.maximum(qy, 0.0)
    return np.sqrt(ax * ax + ay * ay) + np.minimum(np.maximum(qx, qy), 0.0) - r


def polygon_sdf(px, py, verts):
    """凸多边形有向距离，**与顶点绕序无关**（内部恒为负）。

    踩过的坑：早先的实现假定顶点必须逆时针，给了一个反向半平面，
    于是整个上半屏 alpha 被拉满、图标渲染成一片白。
    现在先算无符号边距，再用"三个叉积同号"判内外，绕序随便给。
    """
    n = len(verts)
    dist = None
    crosses = []
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i + 1) % n]
        ex, ey = x2 - x1, y2 - y1
        ll = float(ex * ex + ey * ey)
        t = np.clip(((px - x1) * ex + (py - y1) * ey) / ll, 0.0, 1.0)
        dx = px - (x1 + t * ex)
        dy = py - (y1 + t * ey)
        d = np.sqrt(dx * dx + dy * dy)
        dist = d if dist is None else np.minimum(dist, d)
        crosses.append(ex * (py - y1) - ey * (px - x1))

    # 用多边形面积符号统一"内部同号"
    area2 = 0.0
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i + 1) % n]
        area2 += x1 * y2 - x2 * y1
    sign = 1.0 if area2 >= 0 else -1.0

    inside = np.ones_like(dist, dtype=bool)
    for c in crosses:
        inside &= (c * sign) > 0
    return np.where(inside, -dist, dist)


def cover(sdf, half: float = 0.5):
    return np.clip(half - sdf, 0.0, 1.0)


def over(dst_rgb, dst_a, src_rgb, src_a):
    """src over dst（均为非预乘 float32）。"""
    out_a = src_a + dst_a * (1.0 - src_a)
    safe = np.maximum(out_a, 1e-6)
    out_rgb = (src_rgb * src_a[..., None]
               + dst_rgb * (dst_a * (1.0 - src_a))[..., None]) / safe[..., None]
    return out_rgb, out_a


def to_rgba(rgb, alpha, opaque: bool = False) -> np.ndarray:
    a = np.clip(alpha, 0.0, 1.0).astype(np.float32)
    if opaque:
        a = np.ones_like(a)
    out = np.empty((*a.shape, 4), dtype=np.float32)
    out[..., :3] = np.clip(rgb, 0.0, 1.0)
    out[..., 3] = a
    out[a < 0.004, :3] = 0.0   # 全透明处颜色清零，避免解码后脏边
    return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- 图层绘制

def _scale_for(w: int, h: int, tweak: float = 1.0) -> float:
    return min(w / BASE_W, h / BASE_H) * tweak


def draw_background(w: int, h: int) -> np.ndarray:
    """底色层（不透明）：对角渐变 + 中心柔光 + 暗角。"""
    px, py = _grid(w, h)
    u, v = px / w, py / h

    t = np.clip(u * 0.55 + v * 0.45, 0.0, 1.0)
    c0 = np.array(BG_TOP, dtype=np.float32)
    c1 = np.array(BG_BOT, dtype=np.float32)
    rgb = c0[None, None, :] + (c1 - c0)[None, None, :] * t[..., None]

    gx, gy = w * 0.5, h * 0.56
    gr = min(w, h) * 0.66
    d = np.sqrt((px - gx) ** 2 + (py - gy) ** 2) / gr
    glow = np.clip(1.0 - d, 0.0, 1.0) ** 2.0
    rgb = rgb + np.array(ACCENT, dtype=np.float32)[None, None, :] * (glow * 0.34)[..., None]

    nx = (px / w - 0.5) * 2.0
    ny = (py / h - 0.5) * 2.0
    vig = 1.0 - 0.42 * np.clip(nx * nx + ny * ny, 0.0, 1.0)
    rgb = rgb * vig[..., None]

    return to_rgba(rgb, np.ones((h, w), dtype=np.float32), opaque=True)


def draw_middle(w: int, h: int, tweak: float = 1.0) -> np.ndarray:
    """光晕层（半透明）：视差时落在字形后方，营造深度。

    注意：半径以 min(w,h) 为基准，**不能再乘缩放系数 s**。
    min(w,h) 本身已随画幅线性变化，再乘一次 s 等于缩小两次——
    400x240 上柔光会缩到只剩 2% 面积，视差看起来是平的。
    """
    px, py = _grid(w, h)
    gx, gy = w * 0.5, h * 0.52
    gr = min(w, h) * 0.40 * tweak
    d = np.sqrt((px - gx) ** 2 + (py - gy) ** 2) / max(gr, 1e-6)
    a = np.clip(1.0 - d, 0.0, 1.0) ** 2.4 * 0.34
    rgb = np.ones((h, w, 3), dtype=np.float32) * np.array([0.62, 0.79, 1.0], dtype=np.float32)
    return to_rgba(rgb, a)


def draw_front(w: int, h: int, tweak: float = 1.0) -> np.ndarray:
    """字形层（半透明）：AirPlay 造型——圆角屏框 + 底部实心三角。"""
    px, py = _grid(w, h)
    s = _scale_for(w, h, tweak)
    cx, cy = w / 2.0, h / 2.0

    hw, hh = 296.0 * s, 176.0 * s
    radius = 40.0 * s
    stroke = 26.0 * s

    sdf = rounded_rect_sdf(px, py, cx, cy, hw, hh, radius)
    frame_cov = cover(np.abs(sdf) - stroke * 0.5)

    # 三角：底边贴屏框内侧下缘并与描边连成一体，顶点朝上。
    # 底/高比按 AirPlay 官方标记取 ≈1.45；太窄会看成"山"。
    inner_bottom = cy + hh - stroke * 0.5
    base_y = inner_bottom + 2.0 * s
    tri_h = 146.0 * s
    half_base = tri_h / 2.0 * 1.45
    tri = [(cx - half_base, base_y),
           (cx + half_base, base_y),
           (cx, base_y - tri_h)]
    tri_cov = cover(polygon_sdf(px, py, tri))

    cov = np.maximum(frame_cov * 0.95, tri_cov)

    # 竖向渐变：上纯白、下淡蓝，比纯白更有质感
    g = np.clip((py - (cy - hh)) / max(1.0, 2.0 * hh), 0.0, 1.0)
    top = np.array(GLYPH_TOP, dtype=np.float32)
    bot = np.array(GLYPH_BOT, dtype=np.float32)
    rgb = top[None, None, :] + (bot - top)[None, None, :] * g[..., None]

    return to_rgba(rgb, cov)


def flatten(w: int, h: int) -> np.ndarray:
    """三层合成成单张（用于兜底 PNG 与预览）。"""
    back = draw_background(w, h).astype(np.float32) / 255.0
    mid = draw_middle(w, h).astype(np.float32) / 255.0
    front = draw_front(w, h).astype(np.float32) / 255.0

    rgb = back[..., :3]
    a = back[..., 3]
    rgb, a = over(rgb, a, mid[..., :3], mid[..., 3])
    rgb, a = over(rgb, a, front[..., :3], front[..., 3])
    return to_rgba(rgb, a, opaque=True)


# ---------------------------------------------------------------- 资源落盘

def _info():
    return {"author": "xcode", "version": 1}


def _dump(path: str, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _write_layer(layer_dir: str, files) -> list[str]:
    """一个 imagestacklayer → Content.imageset/{名字@1x.png, @2x.png}。

    files: [(suffix, ndarray), ...]，suffix 如 "@1x"/"@2x"
    """
    _dump(os.path.join(layer_dir, "Contents.json"), {
        "images": [{"filename": "Content.imageset", "idiom": "tv"}],
        "info": _info(),
    })
    iset = os.path.join(layer_dir, "Content.imageset")
    base = os.path.basename(layer_dir).replace(".imagestacklayer", "").lower()

    entries = []
    for suffix, arr in files:
        entries.append({
            "filename": f"{base}{suffix}.png",
            "idiom": "tv",
            "scale": suffix.lstrip("@"),
        })
    _dump(os.path.join(iset, "Contents.json"), {"images": entries, "info": _info()})

    out = []
    for suffix, arr in files:
        h = write_png(os.path.join(iset, f"{base}{suffix}.png"), arr)
        out.append(f"{base}{suffix}.png {h[:12]}")
    return out


def build_stack(stack_dir: str, sizes, tweak: float = 1.0) -> dict:
    """三层视差图标栈。

    sizes: [(w, h, suffix), ...]
    """
    names = ["Front", "Middle", "Back"]
    _dump(os.path.join(stack_dir, "Contents.json"), {
        "layers": [{"filename": f"{n}.imagestacklayer"} for n in names],
        "info": _info(),
    })

    painters = {
        "Front": lambda w, h: draw_front(w, h, tweak),
        "Middle": lambda w, h: draw_middle(w, h, tweak),
        "Back": lambda w, h: draw_background(w, h),
    }

    report = {}
    for n in names:
        files = [(suffix, painters[n](w, h)) for (w, h, suffix) in sizes]
        report[n] = _write_layer(os.path.join(stack_dir, f"{n}.imagestacklayer"), files)
    return report


def build_imageset(path: str, sizes) -> list[str]:
    """sizes: [(w, h, suffix, filename), ...]"""
    _dump(os.path.join(path, "Contents.json"), {
        "images": [{"filename": fn, "idiom": "tv", "scale": s.lstrip("@")}
                   for (_, _, s, fn) in sizes],
        "info": _info(),
    })
    out = []
    for (w, h, suffix, fn) in sizes:
        hh = write_png(os.path.join(path, fn), flatten(w, h))
        out.append(f"{fn} {hh[:12]}")
    return out


def main():
    print(f"资源根目录: {ASSETS}")
    _dump(os.path.join(ASSETS, "Contents.json"), {"info": _info()})

    _dump(os.path.join(BRAND, "Contents.json"), {
        "assets": [
            # 顺序照 Xcode 模板：App Store 栈在前，主屏栈在后。
            # 两者都是 primary-app-icon，靠 size 区分（400x240 / 1280x768）。
            {"filename": "App Icon - App Store.imagestack", "idiom": "tv",
             "role": "primary-app-icon", "size": "1280x768"},
            {"filename": "App Icon.imagestack", "idiom": "tv",
             "role": "primary-app-icon", "size": "400x240"},
            {"filename": "Top Shelf Image Wide.imageset", "idiom": "tv",
             "role": "top-shelf-image-wide", "size": "2320x720"},
            {"filename": "Top Shelf Image.imageset", "idiom": "tv",
             "role": "top-shelf-image", "size": "1920x720"},
        ],
        "info": _info(),
    })

    # 主屏图标：400x240 @1x / 800x480 @2x
    print("\n== App Icon.imagestack (400x240 / 800x480) ==")
    for layer, lines in build_stack(
            os.path.join(BRAND, "App Icon.imagestack"),
            [(400, 240, "@1x"), (800, 480, "@2x")]).items():
        for ln in lines:
            print(f"   {ln}")

    # 商店图标：1280x768（同样三层，视差数据也会带上）
    print("\n== App Icon - App Store.imagestack (1280x768) ==")
    for layer, lines in build_stack(
            os.path.join(BRAND, "App Icon - App Store.imagestack"),
            [(1280, 768, "@1x")]).items():
        for ln in lines:
            print(f"   {ln}")

    # Top Shelf
    print("\n== Top Shelf Image (1920x720 / 3840x1440) ==")
    for ln in build_imageset(os.path.join(BRAND, "Top Shelf Image.imageset"),
                             [(1920, 720, "@1x", "top@1x.png"),
                              (3840, 1440, "@2x", "top@2x.png")]):
        print(f"   {ln}")

    print("\n== Top Shelf Image Wide (2320x720 / 4640x1440) ==")
    for ln in build_imageset(os.path.join(BRAND, "Top Shelf Image Wide.imageset"),
                             [(2320, 720, "@1x", "wide@1x.png"),
                              (4640, 1440, "@2x", "wide@2x.png")]):
        print(f"   {ln}")

    # 单张 1280x768 合成图，随包分发。
    # 注意：tvOS **不**通过这张图显示主屏图标（tvOS 的 CFBundlePrimaryIcon
    # 是字符串，指向 Assets.car 里的图标栈）。它只用于两件事：
    #   1. 给用户直接看的预览；
    #   2. Assets.car 缺失时，让 check_icon.py 能报出"图标渲染过但没编进包"。
    loose = os.path.join(ROOT, "UnCast", "App", "AppIcon.png")
    hl = write_png(loose, flatten(1280, 768))
    print(f"\n参照图  : UnCast/App/AppIcon.png  {hl[:12]}")

    # 预览（不进包）
    preview_dir = os.path.join(ROOT, "dist")
    os.makedirs(preview_dir, exist_ok=True)
    prev = os.path.join(preview_dir, "icon-preview.png")
    hp = write_png(prev, flatten(1280, 768))
    print(f"预览图  : {prev}")
    print(f"sha256  : {hp}")

    # 汇总校验：Back 层与 Top Shelf 必须完全不透明
    print("\n== 不透明度自检（tvOS 硬要求）==")
    for p in [os.path.join(BRAND, "App Icon.imagestack", "Back.imagestacklayer",
                           "Content.imageset", "back@1x.png"),
              os.path.join(BRAND, "Top Shelf Image.imageset", "top@1x.png")]:
        arr = _read_png(p)
        amin = int(arr[..., 3].min())
        status = "OK" if amin == 255 else f"FAIL(min={amin})"
        print(f"   {status}  {os.path.relpath(p, ASSETS)}")
        if amin != 255:
            raise SystemExit("不透明层出现透明像素，tvOS 会拒收")

    print("\n图标资源生成完毕。")


def _read_png(path: str) -> np.ndarray:
    """极简 PNG 读回（只支持本脚本自己写的 8bit RGBA、filter=0）。"""
    with open(path, "rb") as f:
        data = f.read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos = 8
    w = h = None
    idat = b""
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + ln]
        if tag == b"IHDR":
            w, h = struct.unpack(">II", payload[:8])
        elif tag == b"IDAT":
            idat += payload
        elif tag == b"IEND":
            break
        pos += 12 + ln
    raw = zlib.decompress(idat)
    stride = w * 4 + 1
    out = np.empty((h, w, 4), dtype=np.uint8)
    for y in range(h):
        row = raw[y * stride:(y + 1) * stride]
        assert row[0] == 0, "本读回函数只支持 filter=None"
        out[y] = np.frombuffer(row[1:], dtype=np.uint8).reshape(w, 4)
    return out


if __name__ == "__main__":
    sys.exit(main())