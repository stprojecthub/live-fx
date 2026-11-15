"""Generate the multi-resolution LIVE FX Windows application icon."""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


def create_icon(output: Path) -> None:
    scale = 4
    size = 256
    image = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def coords(values):
        return tuple(value * scale for value in values)

    draw.rounded_rectangle(
        coords((12, 12, 244, 244)),
        radius=48 * scale,
        fill="#33271D",
        outline="#E5A642",
        width=7 * scale,
    )
    draw.rounded_rectangle(
        coords((27, 27, 229, 229)),
        radius=36 * scale,
        outline="#72502C",
        width=3 * scale,
    )

    center_y = 128
    bars = ((63, 54), (91, 94), (119, 132), (147, 104), (175, 72), (203, 43))
    for x, height in bars:
        draw.rounded_rectangle(
            coords((x - 7, center_y - height // 2, x + 7, center_y + height // 2)),
            radius=7 * scale,
            fill="#F2B84B",
        )
    draw.line(
        [coords((51, 191)), coords((84, 191)), coords((101, 175)),
         coords((117, 207)), coords((139, 177)), coords((155, 191)),
         coords((209, 191))],
        fill="#DF7541",
        width=5 * scale,
        joint="curve",
    )

    image = image.resize((size, size), Image.Resampling.LANCZOS)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="ICO", sizes=[(16, 16), (24, 24), (32, 32),
                                             (48, 48), (64, 64), (128, 128),
                                             (256, 256)])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    create_icon(args.output)


if __name__ == "__main__":
    main()
