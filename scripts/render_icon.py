#!/usr/bin/env python3
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]


def render(size: int, destination: Path) -> None:
    scale = size / 256
    image = Image.new("RGB", (size, size), "#1f6b4f")
    draw = ImageDraw.Draw(image)

    def box(values):
        return tuple(round(value * scale) for value in values)

    draw.rounded_rectangle(box((0, 0, 255, 255)), radius=round(58 * scale), fill="#1f6b4f")
    draw.ellipse(box((20, 8, 240, 230)), fill="#327f60")
    draw.rounded_rectangle(
        box((42, 55, 193, 188)),
        radius=round(13 * scale),
        fill="#fffdf6",
        outline="#17211d",
        width=max(2, round(9 * scale)),
    )
    draw.ellipse(
        box((71, 77, 105, 111)),
        fill="#e7682f",
        outline="#17211d",
        width=max(2, round(8 * scale)),
    )
    draw.line(
        [box((53, 172))[0:2], box((96, 129))[0:2], box((127, 157))[0:2], box((152, 134))[0:2], box((184, 165))[0:2]],
        fill="#17211d",
        width=max(2, round(10 * scale)),
        joint="curve",
    )
    draw.arc(box((164, 68, 232, 178)), start=270, end=70, fill="#fffdf6", width=max(2, round(13 * scale)))
    draw.line(
        [box((193, 170))[0:2], box((211, 177))[0:2], box((218, 159))[0:2]],
        fill="#fffdf6",
        width=max(2, round(13 * scale)),
        joint="curve",
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, "PNG", optimize=True)


for target_size, relative in [
    (64, "fpk/ICON.PNG"),
    (256, "fpk/ICON_256.PNG"),
    (64, "fpk/ui/images/64.png"),
    (256, "fpk/ui/images/256.png"),
]:
    render(target_size, ROOT / relative)
