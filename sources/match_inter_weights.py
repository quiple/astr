from __future__ import annotations

import argparse
from collections.abc import Callable
from functools import lru_cache
import math
from pathlib import Path
from statistics import fmean

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.recordingPen import RecordingPen
from fontTools.ttLib import TTFont
from ufoLib2 import Font


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INTER_FONT = (
    ROOT / "sources" / "vendor" / "inter" / "docs" / "font-files" / "InterVariable.ttf"
)
MASTER_NAMES = ("ExtraLight", "Light", "Regular", "Text", "Medium", "SemiBold")

# Use straight vertical strokes exclusively so different counters, curves, and
# glyph proportions do not distort the physical stem-width comparison.
KOREAN_STEM_GLYPH = "iCompa-ko"
INTER_STEM_CHARACTERS = "HI"

# Apple mixes SF Pro Latin with SF Pro KR at the same nominal CSS weight, but
# the optical difference is not constant across the range. These anchors are
# the geometric means of the SF Pro Text and Display H/I vertical-stem ratios
# against the SF Pro KR compatibility-jamo ㅣ vertical stem.
APPLE_LATIN_STROKE_BIAS = (
    (200.0, 1.01282051282),
    (300.0, 1.04999295574),
    (400.0, 1.04046242775),
    (500.0, 1.06042455156),
    (600.0, 1.06846271603),
    (700.0, 1.06713780919),
)
TEXT_OPSZ = 14
DISPLAY_OPSZ = 32
OUTPUT_DECIMAL_PLACES = 3


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


def bounding_width(glyph, glyph_set) -> float:
    pen = BoundsPen(glyph_set)
    glyph.draw(pen)
    if pen.bounds is None:
        raise ValueError("sample glyph has no measurable bounds")
    x_min, _y_min, x_max, _y_max = pen.bounds
    return x_max - x_min


def vertical_stem_width(glyph) -> float:
    """Measure the narrow repeated vertical span in an upright H or I."""
    pen = RecordingPen()
    glyph.draw(pen)
    vertical_lengths: dict[float, float] = {}
    points: list[tuple[float, float]] = []
    contour_start = None
    current = None

    def record_edge(start, end) -> None:
        if start is None or end is None:
            return
        x1, y1 = start
        x2, y2 = end
        points.extend((start, end))
        if abs(x1 - x2) < 1e-6 and y1 != y2:
            vertical_lengths[x1] = vertical_lengths.get(x1, 0) + abs(
                y2 - y1
            )

    for operator, operands in pen.value:
        if operator == "moveTo":
            contour_start = current = operands[0]
        elif operator == "lineTo":
            for point in operands:
                record_edge(current, point)
                current = point
        elif operator in ("curveTo", "qCurveTo"):
            curve_points = [point for point in operands if point is not None]
            points.extend(curve_points)
            if curve_points:
                current = curve_points[-1]
        elif operator == "closePath":
            record_edge(current, contour_start)
            contour_start = current = None
        elif operator == "endPath":
            contour_start = current = None

    if not points:
        raise ValueError("stem sample glyph has no measurable outline")
    y_values = [point[1] for point in points]
    height = max(y_values) - min(y_values)
    edge_positions = sorted(
        x
        for x, length in vertical_lengths.items()
        if length >= height * 0.45
    )
    spans = [
        right - left
        for left, right in zip(edge_positions, edge_positions[1:])
        if right > left
    ]
    if not spans:
        raise ValueError("could not identify the H/I vertical stems")
    return min(spans)


def apple_latin_stroke_bias(weight: float) -> float:
    """Interpolate Apple's weight-dependent Latin/Korean optical ratio."""
    first_weight, first_bias = APPLE_LATIN_STROKE_BIAS[0]
    if weight <= first_weight:
        return first_bias

    for (left_weight, left_bias), (right_weight, right_bias) in zip(
        APPLE_LATIN_STROKE_BIAS, APPLE_LATIN_STROKE_BIAS[1:]
    ):
        if weight <= right_weight:
            progress = (weight - left_weight) / (right_weight - left_weight)
            # Stroke scores are logarithmic, so interpolate the ratios in the
            # same space instead of treating their percentages as distances.
            return math.exp(
                math.log(left_bias)
                + progress * (math.log(right_bias) - math.log(left_bias))
            )

    return APPLE_LATIN_STROKE_BIAS[-1][1]


def ufo_stroke_score(path: Path) -> float:
    if not path.is_dir():
        raise FileNotFoundError(f"Astr master not found: {path}")

    font = Font.open(path, lazy=True)
    required_glyphs = (KOREAN_STEM_GLYPH,)
    missing = [name for name in required_glyphs if name not in font]
    if missing:
        raise ValueError(
            f"{path.name} is missing Korean samples: {', '.join(missing)}"
        )

    upm = float(font.info.unitsPerEm or 1000)
    return math.log(
        bounding_width(font[KOREAN_STEM_GLYPH], font) / upm
    )


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
        self.upm = float(self.font["head"].unitsPerEm)

        cmap = self.font.getBestCmap() or {}
        required_characters = INTER_STEM_CHARACTERS
        missing = [
            character
            for character in required_characters
            if ord(character) not in cmap
        ]
        if missing:
            raise ValueError(
                f"Inter font is missing sample characters: {''.join(missing)}"
            )
        self.stem_glyph_names = tuple(
            cmap[ord(character)] for character in INTER_STEM_CHARACTERS
        )

    @lru_cache(maxsize=None)
    def score(self, weight: float, optical_size: int) -> float:
        glyph_set = self.font.getGlyphSet(
            location={"wght": weight, "opsz": optical_size}
        )
        return fmean(
            math.log(vertical_stem_width(glyph_set[name]) / self.upm)
            for name in self.stem_glyph_names
        )

    def find_weight(
        self,
        target_score: float,
        candidate_score: Callable[[float], float],
    ) -> float:
        minimum_score = candidate_score(self.minimum_weight)
        maximum_score = candidate_score(self.maximum_weight)
        if not minimum_score <= target_score <= maximum_score:
            raise ValueError(
                "matching stroke width requires a weight outside Inter's "
                f"{self.minimum_weight:g}–{self.maximum_weight:g} range"
            )

        low = self.minimum_weight
        high = self.maximum_weight
        for _ in range(24):
            middle = (low + high) / 2
            if candidate_score(middle) < target_score:
                low = middle
            else:
                high = middle

        estimate = (low + high) / 2
        return round(
            max(self.minimum_weight, min(self.maximum_weight, estimate)),
            OUTPUT_DECIMAL_PLACES,
        )

    def match_shared(
        self, text_score: float, display_score: float, scale: float
    ) -> float:
        # Uniform Inter scaling multiplies every measured width by this ratio.
        # Text and Display contribute equally to the single shared weight.
        target_score = fmean((text_score, display_score)) - math.log(scale)
        return self.find_weight(
            target_score,
            # Solve the optical correction as part of the weight search. This
            # makes light Inter instances receive less extra weight than the
            # medium and semibold instances, following Apple's measured curve.
            lambda weight: (
                fmean(
                    (
                        self.score(weight, TEXT_OPSZ),
                        self.score(weight, DISPLAY_OPSZ),
                    )
                )
                - math.log(apple_latin_stroke_bias(weight))
            ),
        )


def master_path(master_name: str, display: bool) -> Path:
    prefix = "Astr-Display" if display else "Astr-"
    return ROOT / "sources" / "masters" / f"{prefix}{master_name}.ufo"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Match one shared set of Inter weights to the Astr Text and "
            "Display masters."
        )
    )
    parser.add_argument(
        "scale",
        type=parse_scale,
        help=(
            "uniform Inter scale as a ratio or percentage "
            "(for example: 0.96, 96, or 96%%)"
        ),
    )
    parser.add_argument(
        "--font",
        type=Path,
        default=DEFAULT_INTER_FONT,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    model = InterStrokeModel(args.font)
    text_targets = tuple(
        ufo_stroke_score(master_path(name, display=False)) for name in MASTER_NAMES
    )
    display_targets = tuple(
        ufo_stroke_score(master_path(name, display=True)) for name in MASTER_NAMES
    )

    inter_weights = tuple(
        model.match_shared(text_target, display_target, args.scale)
        for text_target, display_target in zip(text_targets, display_targets)
    )

    formatted_weights = ", ".join(
        f"{weight:.{OUTPUT_DECIMAL_PLACES}f}" for weight in inter_weights
    )
    print(f"SHARED_INTER_WEIGHTS = ({formatted_weights})")


if __name__ == "__main__":
    main()
