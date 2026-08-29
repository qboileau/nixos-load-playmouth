#!/usr/bin/env python3
"""Compose the "spin" variant's frames: the NixOS snowflake as spinning ASCII art.

Each frame re-rasterizes the *rotated* lambda geometry into a fresh character
grid, then draws that grid with a monospace font. The glyphs therefore stay
upright while the art turns under them — rotating one rendered grid instead
would tilt the ;:. themselves, which stops looking like a terminal.

The arms sit at 0/60/…/300 deg and their colors repeat every 120 deg, so the
drawing repeats every 120 deg too: FRAMES frames spanning SPAN_DEG loop
seamlessly into a full turn.

The resulting PNGs are stored in frames/spin/ and committed to the repository so
the theme can be packaged without build-time SVG tooling.

Usage:
    ./generate-spin-frames.py                       # writes frames/spin/
    ./generate-spin-frames.py <source.svg> <out>    # raw mode, used by the flake
"""

import json
import math
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

from PIL import Image as PILImage, ImageDraw, ImageFont

NS = "http://www.w3.org/2000/svg"

VARIANT = "spin"

# Character grid. 43 columns is the art width; the row count follows from the
# font's cell aspect so the envelope stays square in pixels — the logo has to
# stay round while it turns, and never clip.
COLS = 43

# The whole span the frames must cover; see the module docstring.
SPAN_DEG = 120
FRAMES = 36

# Coverage thresholds for the density ramp. Interior cells land on ';', edges
# fade through ':' to '.', which is what makes the shape read as stippled ASCII
# rather than a hard silhouette.
RAMP = [(0.45, ";"), (0.18, ":"), (0.05, ".")]
SUBSAMPLES = 6  # per axis, per cell

# Pixel budgets and spacing, matching the other variants' composition.
LOGO_WIDTH = 512
TEXT_WIDTH = 256
SPACING = 50

WORDMARK = "NixOS"
WORDMARK_COLOR = (255, 255, 255, 255)

FONT_ENV = "SPIN_FONT"
FONT_FALLBACK = "DejaVu Sans Mono"


def font_path() -> str:
    """Monospace TTF to draw with: $SPIN_FONT, else whatever fontconfig picks."""
    env = os.environ.get(FONT_ENV)
    if env:
        return env
    try:
        return subprocess.run(
            ["fc-match", "-f", "%{file}", FONT_FALLBACK],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        print(f"Error: no font. Set ${FONT_ENV} or run from within 'nix develop'.",
              file=sys.stderr)
        sys.exit(1)


def fit_font(path: str, text: str, width: int) -> ImageFont.FreeTypeFont:
    """Largest size whose `text` still fits in `width` pixels."""
    size = 1
    while ImageFont.truetype(path, size + 1).getlength(text) <= width:
        size += 1
    return ImageFont.truetype(path, size)


def stop_color(grad: ET.Element) -> str:
    """The gradient's last (brightest) stop — a flat color for the ASCII art."""
    return grad.findall(f"{{{NS}}}stop")[-1].get("stop-color")


def parse_transform(transform: str) -> tuple[float, float, float, float, float]:
    """(tx, ty, angle, cx, cy) from `translate(tx ty) rotate(a cx cy)`."""
    nums = [float(n) for n in transform.replace(",", " ")
            .replace("translate(", " ").replace("rotate(", " ")
            .replace(")", " ").split()]
    tx, ty, angle, cx, cy = nums
    return tx, ty, angle, cx, cy


def arms(source_svg: str) -> tuple[list[tuple[list[tuple[float, float]], str]],
                                   tuple[float, float]]:
    """The 6 lambda arms as (polygon, color), plus the center they turn about."""
    root = ET.parse(source_svg).getroot()

    defs = root.find(f"{{{NS}}}defs")
    gradients = {g.get("id"): g for g in
                 (defs.findall(f"{{{NS}}}linearGradient") if defs is not None else [])}

    result, centers, colors_at = [], set(), {}
    for polygon in root.findall(f"{{{NS}}}polygon"):
        coords = [float(n) for n in polygon.get("points").split()]
        pts = list(zip(coords[0::2], coords[1::2]))

        tx, ty, angle, cx, cy = parse_transform(polygon.get("transform"))
        color = resolve_fill(polygon.get("fill"), gradients)
        rotated = rotate(pts, angle, (cx, cy))
        result.append(([(x + tx, y + ty) for x, y in rotated], color))
        centers.add((round(cx + tx, 6), round(cy + ty, 6)))
        colors_at.setdefault(angle % SPAN_DEG, set()).add(color)

    if len(centers) != 1:
        sys.exit(f"Error: arms turn about different centers: {sorted(centers)}")

    # Only SPAN_DEG worth of frames is composed, so arms SPAN_DEG apart have to
    # be interchangeable — otherwise the animation would jump when it loops.
    clashing = {angle: sorted(c) for angle, c in colors_at.items() if len(c) > 1}
    if clashing:
        sys.exit(f"Error: arm colors do not repeat every {SPAN_DEG} deg: {clashing}. "
                 f"The frames would not loop.")

    return result, centers.pop()


def resolve_fill(fill: str, gradients: dict[str, ET.Element]) -> str:
    if not fill.startswith("url(#"):
        return fill
    return stop_color(gradients[fill[5:-1]])


def rotate(pts, deg, center):
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    cx, cy = center
    return [((x - cx) * ca - (y - cy) * sa + cx,
             (x - cx) * sa + (y - cy) * ca + cy) for x, y in pts]


def inside(poly, x, y) -> bool:
    """Ray casting; an arm is a simple but concave 9-gon."""
    hit = False
    for i, (x0, y0) in enumerate(poly):
        x1, y1 = poly[(i + 1) % len(poly)]
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) / (y1 - y0) * (x1 - x0):
            hit = not hit
    return hit


def bbox(poly):
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


class Grid:
    """Maps the rotation envelope onto the character cell box."""

    def __init__(self, font: ImageFont.FreeTypeFont, polys, center):
        self.cell_w = round(font.getlength("M"))
        self.cell_h = sum(font.getmetrics())  # ascent + descent
        self.rows = round(COLS * self.cell_w / self.cell_h)  # square, in pixels

        # Nothing can leave this circle while turning, so the envelope never clips.
        self.radius = max(math.dist(p, center) for poly in polys for p in poly)
        side = 2 * self.radius

        self.width, self.height = COLS * self.cell_w, self.rows * self.cell_h
        self.scale = min(self.width, self.height) / side
        self.off = ((self.width - side * self.scale) / 2,
                    (self.height - side * self.scale) / 2)
        self.origin = (center[0] - side / 2, center[1] - side / 2)

    def svg_x(self, px):
        return self.origin[0] + (px - self.off[0]) / self.scale

    def svg_y(self, py):
        return self.origin[1] + (py - self.off[1]) / self.scale

    def cell(self, arms_at, row, col):
        """(char, color) for one cell, or None where nothing covers it.

        Density comes from the arms' *union*, so the seams where they meet stay
        filled; the color comes from whichever arm covers the cell most.
        """
        cx0 = self.svg_x(col * self.cell_w)
        cy0 = self.svg_y(row * self.cell_h)
        cx1 = self.svg_x((col + 1) * self.cell_w)
        cy1 = self.svg_y((row + 1) * self.cell_h)
        near = [(poly, color) for poly, color, (x0, y0, x1, y1) in arms_at
                if x0 <= cx1 and x1 >= cx0 and y0 <= cy1 and y1 >= cy0]
        if not near:
            return None

        step = 1.0 / SUBSAMPLES
        hits, per_color = 0, {}
        for sy in range(SUBSAMPLES):
            y = self.svg_y((row + (sy + 0.5) * step) * self.cell_h)
            for sx in range(SUBSAMPLES):
                x = self.svg_x((col + (sx + 0.5) * step) * self.cell_w)
                covered = [c for poly, c in near if inside(poly, x, y)]
                for c in covered:
                    per_color[c] = per_color.get(c, 0) + 1
                hits += bool(covered)

        coverage = hits / SUBSAMPLES ** 2
        for threshold, char in RAMP:
            if coverage >= threshold:
                return char, max(per_color, key=per_color.get)
        return None


def frame(grid: Grid, arms_, center, angle):
    """The character grid at `angle`, as rows of (char, color) or None."""
    arms_at = [(poly, color, bbox(poly)) for poly, color in
               ((rotate(p, angle, center), c) for p, c in arms_)]
    return [[grid.cell(arms_at, row, col) for col in range(COLS)]
            for row in range(grid.rows)]


def compose_frames(source_svg: str, output_dir: str) -> None:
    arms_, center = arms(source_svg)
    path = font_path()
    font = fit_font(path, "M" * COLS, LOGO_WIDTH)
    wordmark_font = fit_font(path, WORDMARK, TEXT_WIDTH)
    grid = Grid(font, [p for p, _ in arms_], center)

    left, top, right, bottom = wordmark_font.getbbox(WORDMARK)
    text_w, text_h = right - left, bottom - top

    canvas_w = max(grid.width, text_w)
    canvas_h = grid.height + SPACING + text_h
    logo_x = (canvas_w - grid.width) // 2
    text_x = (canvas_w - text_w) // 2 - left
    text_y = grid.height + SPACING - top

    print(f"  Font: {path}")
    print(f"  Grid: {COLS}x{grid.rows} cells of {grid.cell_w}x{grid.cell_h} px")
    print(f"  Frames: {FRAMES} over {SPAN_DEG} deg ({SPAN_DEG / FRAMES:.2f} deg apart)")

    first = None
    for step in range(FRAMES):
        cells = frame(grid, arms_, center, step * SPAN_DEG / FRAMES)
        first = first or cells

        image = PILImage.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        for row, line in enumerate(cells):
            for col, cell in enumerate(line):
                if cell:
                    char, color = cell
                    draw.text((logo_x + col * grid.cell_w, row * grid.cell_h),
                              char, font=font, fill=color)
        draw.text((text_x, text_y), WORDMARK, font=wordmark_font,
                  fill=WORDMARK_COLOR)

        image.save(os.path.join(output_dir, f"frame-{step}.png"))
        print(f"  Composed: frame-{step}.png")

    print(f"  Total frames: {FRAMES}")
    print("\n".join("".join(c[0] if c else " " for c in line) for line in first))


def main() -> None:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    variants = json.load(open(os.path.join(script_dir, "variants.json")))
    if variants[VARIANT]["numFrames"] != FRAMES:
        sys.exit(f"Error: variants.json says {variants[VARIANT]['numFrames']} frames, "
                 f"this script composes {FRAMES}.")

    # Raw mode: generate-spin-frames.py <source.svg> <output_dir>
    # Used by the Nix mkSpinFrames derivation.
    if len(sys.argv) == 3 and sys.argv[1].endswith(".svg"):
        source_svg, output_dir = sys.argv[1], sys.argv[2]
    elif len(sys.argv) == 1:
        source_svg = os.path.join(script_dir, "assets", variants[VARIANT]["svg"])
        output_dir = os.path.join(script_dir, "frames", VARIANT)
    else:
        sys.exit(f"Usage: {sys.argv[0]} [<source.svg> <output_dir>]")

    os.makedirs(output_dir, exist_ok=True)
    print(f"==> Composing {VARIANT} frames from {os.path.basename(source_svg)}")
    compose_frames(source_svg, output_dir)
    print(f"==> Done: {output_dir}/")


if __name__ == "__main__":
    main()
