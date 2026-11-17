#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 make_icon.py 的所有主题渲染成一张对比图，供挑选配色。

为什么不直接生成四套 brandassets：那会把仓库搞乱，而用户只需要看效果。
这里只在内存里画出每个主题的合成图，拼成网格落盘。

用法:
    python3 scripts/icon_themes_preview.py
    → dist/theme-preview.png
"""
from __future__ import annotations

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import make_icon as M  # noqa: E402

ROOT = M.ROOT
OUT = os.path.join(ROOT, "dist", "theme-preview.png")

# 每个主题画两个尺寸：主屏实际大小 400x240，以及 2 倍放大
TILE_W, TILE_H = 400, 240
COLS = 2
PAD = 16


def rounded_tile(img: np.ndarray, radius: int = 22) -> np.ndarray:
    """把矩形图裁成圆角（主屏图标实际是圆角矩形，预览要对得上）。"""
    h, w = img.shape[:2]
    px, py = M._grid(w, h)
    sdf = M.rounded_rect_sdf(px, py, w / 2.0, h / 2.0,
                             w / 2.0 - 1, h / 2.0 - 1, radius)
    a = M.cover(sdf)
    out = img.copy()
    out[..., 3] = (out[..., 3].astype(np.float32) * a).clip(0, 255).astype(np.uint8)
    return out


def main() -> int:
    keys = list(M.THEMES.keys())
    rows = (len(keys) + COLS - 1) // COLS

    W = COLS * TILE_W + (COLS + 1) * PAD
    H = rows * TILE_H + (rows + 1) * PAD

    # 中性深灰底（主屏背景大概这个亮度），这样能看出图标在 TV 上是否醒目
    canvas = np.zeros((H, W, 4), dtype=np.uint8)
    canvas[..., :3] = 26
    canvas[..., 3] = 255

    for i, k in enumerate(keys):
        theme = M.THEMES[k]
        tile = rounded_tile(M.flatten(TILE_W, TILE_H, theme))
        r, c = divmod(i, COLS)
        y = PAD + r * (TILE_H + PAD)
        x = PAD + c * (TILE_W + PAD)

        # alpha 混合贴上去（圆角处要是透明的）
        dst = canvas[y:y + TILE_H, x:x + TILE_W, :3].astype(np.float32) / 255.0
        src = tile[..., :3].astype(np.float32) / 255.0
        a = tile[..., 3].astype(np.float32) / 255.0
        mix = src * a[..., None] + dst * (1.0 - a[..., None])
        canvas[y:y + TILE_H, x:x + TILE_W, :3] = (
            np.clip(mix, 0, 1) * 255.0 + 0.5).astype(np.uint8)

        print(f"  {k:<10} {theme['label']}")

    M.write_png(OUT, canvas)
    print(f"\n对比图: {OUT}  ({W}x{H})")
    print("  布局: 左上=%s 右上=%s 左下=%s 右下=%s" % tuple(keys))
    return 0


if __name__ == "__main__":
    sys.exit(main())