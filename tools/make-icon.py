"""Generate the multi-resolution TUSKFiZZY Windows icon from its simple vector design."""
from __future__ import annotations

from math import cos, pi, sin
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "shell" / "assets" / "icon.ico"
SCALE = 4
SIZE = 256


def star_points(cx: float, cy: float, outer: float, inner: float) -> list[tuple[float, float]]:
    points = []
    for index in range(8):
        radius = outer if index % 2 == 0 else inner
        angle = -pi / 2 + index * pi / 4
        points.append((cx + cos(angle) * radius, cy + sin(angle) * radius))
    return points


def render() -> Image.Image:
    side = SIZE * SCALE
    image = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4*SCALE, 4*SCALE, 252*SCALE, 252*SCALE), radius=54*SCALE, fill="#171322")
    draw.ellipse((34*SCALE, 34*SCALE, 222*SCALE, 222*SCALE), outline="#8D61E8", width=9*SCALE)
    points = [(x*SCALE, y*SCALE) for x, y in star_points(128, 128, 91, 29)]
    draw.polygon(points, fill="#B58CFF")
    draw.ellipse((116*SCALE, 116*SCALE, 140*SCALE, 140*SCALE), fill="#F5F1FF")
    return image.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    render().save(OUTPUT, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"Generated {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
