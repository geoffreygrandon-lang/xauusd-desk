#!/usr/bin/env python3
"""Rend les icônes PWA à partir du même dessin que web/logo.svg."""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
ASSETS = ROOT / "assets"
BG = (13, 13, 18, 255)
GOLD = (232, 196, 122, 255)
DIM = (154, 122, 69, 255)


def _candle(draw, size, cx, wick_top, wick_bot, body_top, body_bot, width, color):
    x = cx * size
    half = width * size / 2
    draw.line([(x, wick_top * size), (x, wick_bot * size)], fill=color, width=max(2, int(size * 0.02)))
    top = min(body_top, body_bot) * size
    bot = max(body_top, body_bot) * size
    draw.rectangle([x - half, top, x + half, bot], fill=color)


def render(size, maskable=False):
    image = Image.new("RGBA", (size, size), BG)
    draw = ImageDraw.Draw(image)
    if maskable:
        # Zone sûre au centre : le système rogne les bords.
        scale = 0.72
        ox = (1 - scale) / 2
        def n(value):
            return ox + value * scale
    else:
        radius = int(size * 0.22)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
        plate = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(plate).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=BG)
        image = plate
        draw = ImageDraw.Draw(image)
        def n(value):
            return value
        inset = int(size * 0.08)
        draw.rounded_rectangle(
            (inset, inset, size - 1 - inset, size - 1 - inset),
            radius=int(radius * 0.75),
            outline=(*GOLD[:3], 72),
            width=max(2, size // 64),
        )
    _candle(draw, size, n(0.30), n(0.42), n(0.70), n(0.48), n(0.64), 0.10 if not maskable else 0.08, DIM)
    _candle(draw, size, n(0.50), n(0.22), n(0.78), n(0.36), n(0.64), 0.22 if not maskable else 0.16, GOLD)
    _candle(draw, size, n(0.70), n(0.34), n(0.66), n(0.38), n(0.56), 0.10 if not maskable else 0.08, GOLD)
    return image


def main():
    WEB.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)
    outputs = {
        WEB / "icon-192.png": render(192),
        WEB / "icon-512.png": render(512),
        WEB / "icon-180.png": render(180),
        WEB / "favicon.png": render(32),
        WEB / "icon-maskable-512.png": render(512, maskable=True),
        ASSETS / "logo-preview.png": render(512),
    }
    for path, image in outputs.items():
        image.save(path, "PNG")
        print(path.name, image.size)


if __name__ == "__main__":
    main()
