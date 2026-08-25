from __future__ import annotations

import argparse
from functools import lru_cache
import math
from pathlib import Path
from statistics import fmean

from fontTools.pens.areaPen import AreaPen
from fontTools.pens.perimeterPen import PerimeterPen
from fontTools.ttLib import TTFont


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INTER_FONT = (
    ROOT / "sources" / "vendor" / "inter" / "docs" / "font-files" / "InterVariable.ttf"
)

# Inter weights matched visually against the six Asta Sans masters at 100% scale.
MASTER_SPECS = (
    ("ExtraLight", 225),
    ("Light", 325),
    ("Regular", 400),
    ("Text", 425),
    ("Medium", 475),
    ("SemiBold", 550),
)

# A mixture of straight, round, uppercase, lowercase, and counter-bearing forms.
SAMPLE_CHARACTERS = "HInoOSB8"
OPTICAL_SIZES = (14, 32)


def parse_scale(value: str) -> float:
    text = value.strip()
    is_percent = text.endswith("%")
    if is_percent:
        text = text[:-1]
    try:
        scale = float(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid scale: {value!r}") from error

    if is_percent or scale > 10:
        scale /= 100
    if not math.isfinite(scale) or scale <= 0:
        raise argparse.ArgumentTypeError("scale must be greater than zero")
    return scale


class InterStrokeModel:
    def __init__(self, font_path: Path) -> None:
        if not font_path.is_file():
            raise FileNotFoundError(
                f"Inter variable font not found: {font_path}\n"
                "Run `make sync-inter` first or pass --font."
            )

        self.font = TTFont(font_path)
        axes = {axis.axisTag: axis for axis in self.font["fvar"].axes}
        if "wght" not in axes or "opsz" not in axes:
            raise ValueError("Inter font must contain both wght and opsz axes")
        self.minimum_weight = float(axes["wght"].minValue)
        self.maximum_weight = float(axes["wght"].maxValue)

        cmap = self.font.getBestCmap() or {}
        missing = [character for character in SAMPLE_CHARACTERS if ord(character) not in cmap]
        if missing:
            raise ValueError(f"Inter font is missing sample characters: {''.join(missing)}")
        self.glyph_names = tuple(cmap[ord(character)] for character in SAMPLE_CHARACTERS)

    @lru_cache(maxsize=None)
    def score(self, weight: float) -> float:
        """Return the geometric mean of representative average-stroke widths."""
        logarithmic_strokes: list[float] = []
        for optical_size in OPTICAL_SIZES:
            glyph_set = self.font.getGlyphSet(location={"wght": weight, "opsz": optical_size})
            for glyph_name in self.glyph_names:
                glyph = glyph_set[glyph_name]

                area_pen = AreaPen(glyph_set)
                glyph.draw(area_pen)
                perimeter_pen = PerimeterPen(glyph_set, tolerance=0.01)
                glyph.draw(perimeter_pen)

                # For a long rectangular stem, 2A/P approaches its thickness.
                stroke = 2 * abs(area_pen.value) / perimeter_pen.value
                logarithmic_strokes.append(math.log(stroke))

        return fmean(logarithmic_strokes)

    def match_scaled_weight(self, reference_weight: int, scale: float) -> int:
        # Uniform geometry scaling multiplies 2A/P by the same scale factor.
        target_score = self.score(float(reference_weight)) - math.log(scale)
        minimum_score = self.score(self.minimum_weight)
        maximum_score = self.score(self.maximum_weight)
        if not minimum_score <= target_score <= maximum_score:
            raise ValueError(
                f"scale {scale:g} requires a weight outside Inter's "
                f"{self.minimum_weight:g}–{self.maximum_weight:g} range"
            )

        low = self.minimum_weight
        high = self.maximum_weight
        for _ in range(24):
            middle = (low + high) / 2
            if self.score(middle) < target_score:
                low = middle
            else:
                high = middle

        estimate = (low + high) / 2
        candidates = {
            max(int(self.minimum_weight), min(int(self.maximum_weight), math.floor(estimate))),
            max(int(self.minimum_weight), min(int(self.maximum_weight), math.ceil(estimate))),
        }
        return min(candidates, key=lambda weight: abs(self.score(float(weight)) - target_score))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calculate Inter master weights that preserve stroke weight after scaling."
    )
    parser.add_argument(
        "scale",
        type=parse_scale,
        help="uniform Inter scale as a ratio or percentage (for example: 0.96, 96, or 96%%)",
    )
    parser.add_argument(
        "--font",
        type=Path,
        default=DEFAULT_INTER_FONT,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    model = InterStrokeModel(args.font)
    matched = tuple(
        model.match_scaled_weight(reference_weight, args.scale)
        for _name, reference_weight in MASTER_SPECS
    )

    print(f"Inter scale: {args.scale * 100:g}%")
    for (name, reference_weight), matched_weight in zip(MASTER_SPECS, matched):
        difference = matched_weight - reference_weight
        print(f"{name:<10} {matched_weight:>3}  ({difference:+d} from {reference_weight})")
    print(f"WEIGHTS = {matched}")


if __name__ == "__main__":
    main()
