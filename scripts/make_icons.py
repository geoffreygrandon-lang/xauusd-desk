#!/usr/bin/env python3
"""Icônes PWA sans dépendance externe."""

import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "web"


def write_png(path, size, pixel):
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            raw.extend(pixel(x, y, size))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")
    path.write_bytes(png)


def icon_pixel(x, y, size):
    nx = (x + 0.5) / size
    ny = (y + 0.5) / size
    # fond arrondi
    margin = 0.08
    radius = 0.18
    left, top, right, bottom = margin, margin, 1 - margin, 1 - margin
    inside = left <= nx <= right and top <= ny <= bottom
    if inside:
        dx = 0
        if nx < left + radius:
            dx = left + radius - nx
        elif nx > right - radius:
            dx = nx - (right - radius)
        dy = 0
        if ny < top + radius:
            dy = top + radius - ny
        elif ny > bottom - radius:
            dy = ny - (bottom - radius)
        if dx * dx + dy * dy > radius * radius:
            inside = False
    if not inside:
        return (0, 0, 0, 0)
    # carte
    color = (21, 21, 28, 255)
    cx, cy, radius_arc = 0.50, 0.60, 0.26
    dx = nx - cx
    dy = ny - cy
    dist = math.hypot(dx, dy)
    # piste du demi-cercle
    if dy <= 0.012 and 0.205 < dist < 0.275:
        # angle 0 à gauche, 1 à droite
        progress = (math.atan2(-dy, dx) ) / math.pi  # 1 at left? atan2(y, x) with y up
        # dx negative = left. We want a purple arc covering ~72% from the left.
        # angle from +pi (left) to 0 (right): use atan2(-dy, -dx) ... 
        angle = math.atan2(-dy, -dx)  # 0 at left, pi at right, upper half positive? 
        # simpler: fraction along the semicircle using dx
        frac = (dx / radius_arc + 1) / 2  # 0 left, 1 right
        if 0 <= frac <= 0.72:
            color = (124, 108, 255, 255)
        else:
            color = (42, 42, 54, 255)
    # point en bout d'arc
    end_angle = math.pi * (1 - 0.72)
    ex = cx + math.cos(math.pi - end_angle) * 0.24
    ey = cy - math.sin(end_angle) * 0.24
    if math.hypot(nx - ex, ny - ey) < 0.035:
        color = (61, 220, 151, 255)
    # petit trait de prix
    if 0.34 < nx < 0.66 and 0.74 < ny < 0.78:
        color = (243, 243, 247, 255)
    return color


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    for size, name in ((180, "icon-180.png"), (192, "icon-192.png"), (512, "icon-512.png")):
        write_png(ROOT / name, size, icon_pixel)
        print(name, size)


if __name__ == "__main__":
    main()
