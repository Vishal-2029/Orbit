#!/usr/bin/env python3
"""Synthesise a realistic overlapping 8-photo set for end-to-end testing of
the CV worker's panorama stitch.

Renders one wide "scene" image (gradient sky + a strip of coloured shapes
scattered along it, like objects around a room) and crops 8 overlapping
windows out of it, left to right, each offset by less than a full window
width so consecutive photos share ~40% overlap - enough for cv2.Stitcher to
find matching features.

Usage: python3 make_test_photos.py [output_dir]
"""
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else "/tmp/orbit_test_photos"

SCENE_W = 4000
SCENE_H = 900
PHOTO_W = 1200
PHOTO_H = 900
N_PHOTOS = 8
OVERLAP = 0.45  # fraction of photo width shared with the next


def build_scene():
    # Horizontal gradient sky, distinct per-column colour so features are
    # trackable, plus a band of varied shapes for texture the stitcher can
    # match keypoints on.
    arr = np.zeros((SCENE_H, SCENE_W, 3), dtype=np.uint8)
    for x in range(SCENE_W):
        t = x / SCENE_W
        r = int(30 + 180 * t)
        g = int(120 + 80 * (1 - t))
        b = int(200 - 150 * t)
        arr[:, x] = (r, g, b)

    img = Image.fromarray(arr, "RGB")
    draw = ImageDraw.Draw(img)

    rng = np.random.default_rng(42)
    colors = [(255, 80, 80), (80, 255, 120), (255, 220, 60), (80, 160, 255),
              (255, 130, 220), (200, 255, 255), (255, 255, 255), (40, 40, 40)]
    # Scatter shapes across the whole scene width with decent density so every
    # crop window contains several distinctive features.
    n_shapes = 140
    for i in range(n_shapes):
        cx = rng.uniform(0, SCENE_W)
        cy = rng.uniform(SCENE_H * 0.15, SCENE_H * 0.85)
        size = rng.uniform(25, 90)
        color = tuple(int(c) for c in colors[i % len(colors)])
        shape = i % 3
        if shape == 0:
            draw.ellipse([cx - size, cy - size, cx + size, cy + size], fill=color, outline=(0, 0, 0))
        elif shape == 1:
            draw.rectangle([cx - size, cy - size, cx + size, cy + size], fill=color, outline=(0, 0, 0))
        else:
            draw.polygon([(cx, cy - size), (cx - size, cy + size), (cx + size, cy + size)],
                         fill=color, outline=(0, 0, 0))

    # A horizon line and floor texture line, for a stable straight-line feature.
    draw.line([(0, SCENE_H * 0.7), (SCENE_W, SCENE_H * 0.7)], fill=(0, 0, 0), width=3)
    for x in range(0, SCENE_W, 60):
        draw.line([(x, SCENE_H * 0.7), (x + 20, SCENE_H)], fill=(20, 20, 20), width=2)

    return img


def crop_windows(scene: Image.Image):
    step = int(PHOTO_W * (1 - OVERLAP))
    max_x = SCENE_W - PHOTO_W
    xs = [min(i * step, max_x) for i in range(N_PHOTOS)]
    windows = []
    for x in xs:
        crop = scene.crop((x, 0, x + PHOTO_W, PHOTO_H))
        windows.append(crop)
    return windows


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    scene = build_scene()
    windows = crop_windows(scene)
    step_deg = 360.0 / N_PHOTOS
    paths = []
    for i, win in enumerate(windows):
        path = os.path.join(OUT_DIR, f"{i:03d}.jpg")
        win.save(path, "JPEG", quality=92)
        paths.append(path)
        print(f"wrote {path} yaw={i * step_deg:.1f}")
    print(f"\n{len(paths)} photos written to {OUT_DIR}")


if __name__ == "__main__":
    main()
