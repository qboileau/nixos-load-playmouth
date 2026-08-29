#!/usr/bin/env python3
"""Extract individual lambda arms and NixOS text from a source logo SVG,
rasterize the components, and compose animation frames for the Plymouth theme.

Each lambda transition has FADE_STEPS sub-frames with increasing/decreasing
opacity for smooth animation. The animation cycle is:

  Frame 0:                          all lambdas hidden (text only)
  Frames 1 … 6*FADE_STEPS:         lambdas fade in one-by-one
  Frames 6*FADE_STEPS+1 … 12*FADE_STEPS: lambdas fade out one-by-one

The resulting PNGs are stored in frames/<variant>/ and committed to the
repository so the theme can be packaged without build-time SVG tooling.

Covers the "reveal" variants of variants.json; the "spin" one is composed by
generate-spin-frames.py instead.

Usage:
    ./generate-frames.py                        # all reveal variants
    ./generate-frames.py default                # single variant
    ./generate-frames.py /path/to/logo.svg out/ # raw split+rasterize+compose mode
"""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from PIL import Image as PILImage, ImageEnhance

NS = "http://www.w3.org/2000/svg"

LAMBDA_VIEWBOX = "-1200 -1050 2400 2100"
TEXT_VIEWBOX = "-10 -1366 4486 1401"

LAMBDA_WIDTH = 512
TEXT_WIDTH = 256

# Spacing between snowflake bottom and text top (pixels)
SPACING = 50

# Reveal order — clockwise starting from top-left
# Indices into the lambda array (0-based): lambda-3, lambda-4, lambda-5, lambda-6, lambda-1, lambda-2
REVEAL_ORDER = [2, 3, 4, 5, 0, 1]

# Number of intermediate opacity steps per lambda transition
FADE_STEPS = 3

# Total frames: 1 (base) + 6 * FADE_STEPS (appear) + 6 * FADE_STEPS (disappear)
NUM_FRAMES = 1 + 12 * FADE_STEPS

STYLE = "reveal"

VARIANTS = json.load(open(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "variants.json")))
SOURCE_SVGS = {name: v["svg"] for name, v in VARIANTS.items() if v["style"] == STYLE}

for name, variant in VARIANTS.items():
    if variant["style"] == STYLE and variant["numFrames"] != NUM_FRAMES:
        sys.exit(f"Error: variants.json says {name} has {variant['numFrames']} "
                 f"frames, this script composes {NUM_FRAMES}.")


def _make_svg(viewbox: str) -> ET.Element:
    """Create an SVG root element with the given viewBox.
    
    We write the xmlns attribute manually in the serialization step
    to avoid ElementTree's duplicate namespace issue with rsvg-convert.
    """
    svg = ET.Element("svg")
    svg.set("viewBox", viewbox)
    return svg


def _write_svg(tree: ET.ElementTree, path: str) -> None:
    """Write SVG tree, injecting the xmlns declaration."""
    raw = ET.tostring(tree.getroot(), encoding="unicode")
    # Inject xmlns if not present (ElementTree drops it without register_namespace)
    if 'xmlns="' not in raw:
        raw = raw.replace("<svg ", f'<svg xmlns="{NS}" ', 1)
    with open(path, "w") as f:
        f.write(raw)


def split_svg(source_svg: str, output_dir: str) -> None:
    """Split a NixOS logo SVG into 6 lambda SVGs + 1 text SVG."""
    tree = ET.parse(source_svg)
    root = tree.getroot()

    # Collect gradient definitions (may be empty for "white" variant)
    defs_elem = root.find(f"{{{NS}}}defs")
    gradients = {}
    if defs_elem is not None:
        for grad in defs_elem.findall(f"{{{NS}}}linearGradient"):
            gradients[grad.get("id")] = grad

    # Extract each polygon (lambda arm)
    polygons = root.findall(f"{{{NS}}}polygon")
    for i, polygon in enumerate(polygons):
        svg = _make_svg(LAMBDA_VIEWBOX)

        new_polygon = copy.deepcopy(polygon)
        fill = polygon.get("fill", "")

        if fill.startswith("url(#"):
            grad_id = fill[5:-1]
            if grad_id in gradients:
                new_defs = ET.SubElement(svg, "defs")
                new_grad = copy.deepcopy(gradients[grad_id])
                new_grad.set("id", "grad")
                new_defs.append(new_grad)
                new_polygon.set("fill", "url(#grad)")

        svg.append(new_polygon)

        out_path = os.path.join(output_dir, f"lambda-{i + 1}.svg")
        _write_svg(ET.ElementTree(svg), out_path)
        print(f"  Extracted: lambda-{i + 1}.svg")

    # Extract text (the <g> with <path> children)
    g_elem = root.find(f"{{{NS}}}g")
    if g_elem is not None:
        svg = _make_svg(TEXT_VIEWBOX)

        for path in g_elem.findall(f"{{{NS}}}path"):
            svg.append(copy.deepcopy(path))

        out_path = os.path.join(output_dir, "text.svg")
        _write_svg(ET.ElementTree(svg), out_path)
        print("  Extracted: text.svg")


def rasterize(svg_path: str, png_path: str, width: int) -> None:
    """Rasterize an SVG to PNG using rsvg-convert."""
    subprocess.run(
        ["rsvg-convert", "-w", str(width), svg_path, "-o", png_path],
        check=True,
    )


def compose_frames(components_dir: str, output_dir: str) -> None:
    """Compose animation frames from rasterized lambda + text PNGs.

    Each lambda transition is split into FADE_STEPS sub-frames with
    increasing opacity (appear) or decreasing opacity (disappear).
    """
    lambdas = []
    for i in range(1, 7):
        img = PILImage.open(
            os.path.join(components_dir, f"lambda-{i}.png")
        ).convert("RGBA")
        lambdas.append(img)
    text = PILImage.open(
        os.path.join(components_dir, "text.png")
    ).convert("RGBA")

    logo_w, logo_h = lambdas[0].size
    text_w, text_h = text.size

    canvas_w = max(logo_w, text_w)
    canvas_h = logo_h + SPACING + text_h

    # Center positions
    logo_x = (canvas_w - logo_w) // 2
    text_x = (canvas_w - text_w) // 2
    text_y = logo_h + SPACING

    def paste_lambda_with_opacity(frame, idx, opacity):
        """Paste a lambda onto the frame with the given opacity (0.0–1.0)."""
        if opacity <= 0:
            return
        lam = lambdas[idx].copy()
        # Scale the alpha channel
        r, g, b, a = lam.split()
        a = a.point(lambda x: int(x * opacity))
        lam = PILImage.merge("RGBA", (r, g, b, a))
        frame.paste(lam, (logo_x, 0), lam)

    # Build a list of (lambda_index, opacity) for each of the 6 slots at each frame
    frame_num = 0
    for step in range(NUM_FRAMES):
        frame = PILImage.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

        if step == 0:
            # All hidden — just text
            pass
        elif step <= 6 * FADE_STEPS:
            # Appear phase
            # Which lambda slot is currently fading in (0-based)
            slot = (step - 1) // FADE_STEPS
            sub = (step - 1) % FADE_STEPS  # 0 … FADE_STEPS-1

            # All previously fully-revealed lambdas
            for s in range(slot):
                paste_lambda_with_opacity(frame, REVEAL_ORDER[s], 1.0)

            # Current lambda fading in
            opacity = (sub + 1) / FADE_STEPS
            paste_lambda_with_opacity(frame, REVEAL_ORDER[slot], opacity)
        else:
            # Disappear phase
            offset = step - 6 * FADE_STEPS - 1
            slot = offset // FADE_STEPS
            sub = offset % FADE_STEPS

            # All lambdas that haven't started disappearing yet
            for s in range(slot + 1, 6):
                paste_lambda_with_opacity(frame, REVEAL_ORDER[s], 1.0)

            # Current lambda fading out
            opacity = 1.0 - (sub + 1) / FADE_STEPS
            paste_lambda_with_opacity(frame, REVEAL_ORDER[slot], opacity)

        # Paste text
        frame.paste(text, (text_x, text_y), text)

        out_path = os.path.join(output_dir, f"frame-{step}.png")
        frame.save(out_path)
        print(f"  Composed: frame-{step}.png")

    print(f"  Total frames: {NUM_FRAMES}")


def process_variant(variant: str, assets_dir: str, frames_dir: str) -> None:
    """Split source SVG, rasterize, and compose animation frames."""
    source_svg = os.path.join(assets_dir, SOURCE_SVGS[variant])
    if not os.path.isfile(source_svg):
        print(f"Error: source SVG not found: {source_svg}", file=sys.stderr)
        sys.exit(1)

    output_dir = os.path.join(frames_dir, variant)
    os.makedirs(output_dir, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        print(f"==> Splitting {variant} source SVG into components")
        split_svg(source_svg, tmp)

        print(f"==> Rasterizing component PNGs for: {variant}")
        for i in range(1, 7):
            rasterize(
                os.path.join(tmp, f"lambda-{i}.svg"),
                os.path.join(tmp, f"lambda-{i}.png"),
                LAMBDA_WIDTH,
            )
        rasterize(
            os.path.join(tmp, "text.svg"),
            os.path.join(tmp, "text.png"),
            TEXT_WIDTH,
        )

        print(f"==> Composing animation frames for: {variant}")
        compose_frames(tmp, output_dir)

    print(f"==> Done: {output_dir}/")
    print("   ", " ".join(sorted(os.listdir(output_dir))))


def main() -> None:
    # Raw mode: generate-frames.py <source.svg> <output_dir>
    # Used by Nix mkFrames derivation.
    if len(sys.argv) == 3 and sys.argv[1].endswith(".svg"):
        source_svg = sys.argv[1]
        output_dir = sys.argv[2]
        os.makedirs(output_dir, exist_ok=True)

        with tempfile.TemporaryDirectory() as tmp:
            print(f"==> Splitting {os.path.basename(source_svg)}")
            split_svg(source_svg, tmp)

            print("==> Rasterizing component PNGs")
            for i in range(1, 7):
                rasterize(
                    os.path.join(tmp, f"lambda-{i}.svg"),
                    os.path.join(tmp, f"lambda-{i}.png"),
                    LAMBDA_WIDTH,
                )
            rasterize(
                os.path.join(tmp, "text.svg"),
                os.path.join(tmp, "text.png"),
                TEXT_WIDTH,
            )

            print("==> Composing animation frames")
            compose_frames(tmp, output_dir)

        print(f"==> Done: {output_dir}/")
        return

    # Check rsvg-convert is available
    if not shutil.which("rsvg-convert"):
        print("Error: 'rsvg-convert' not found. Run from within 'nix develop'.",
              file=sys.stderr)
        sys.exit(1)

    # Variant mode: generate-frames.py [variant ...]
    script_dir = os.path.dirname(os.path.abspath(__file__))
    assets_dir = os.path.join(script_dir, "assets")
    frames_dir = os.path.join(script_dir, "frames")

    variants = sys.argv[1:] if len(sys.argv) > 1 else list(SOURCE_SVGS.keys())

    for v in variants:
        if v not in SOURCE_SVGS:
            print(f"Error: unknown variant '{v}'. "
                  f"Choose: {', '.join(SOURCE_SVGS.keys())}", file=sys.stderr)
            sys.exit(1)

    for v in variants:
        process_variant(v, assets_dir, frames_dir)


if __name__ == "__main__":
    main()
