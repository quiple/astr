from __future__ import annotations

import argparse
from collections.abc import Callable
from functools import lru_cache
import math
from pathlib import Path
from statistics import fmean

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.recordingPen import DecomposingRecordingPen, RecordingPen
from fontTools.ttLib import TTFont
from ufoLib2 import Font


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INTER_FONT = (
    ROOT / "sources" / "vendor" / "inter" / "docs" / "font-files" / "InterVariable.ttf"
)
MASTER_NAMES = ("ExtraLight", "Light", "Regular", "Text", "Medium", "SemiBold")

# Compare the round strokes of the ieung in 아/오/의/이 with Inter's O/o/0.
# These samples cover right-side, lower, and compound-vowel layouts. For each
# ring, left/right thicknesses form the vertical score and top/bottom
# thicknesses form the horizontal score.
KOREAN_RING_GLYPHS = ("a-ko", "o-ko", "yi-ko", "i-ko")
INTER_RING_CHARACTERS = "Oo0"
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


Bounds = tuple[float, float, float, float]


def contour_bounds(glyph, glyph_set) -> list[Bounds]:
    """Return curve-aware bounds for each decomposed contour in a glyph."""
    pen = DecomposingRecordingPen(glyph_set)
    glyph.draw(pen)
    bounds: list[Bounds] = []
    contour: RecordingPen | None = None

    for operator, operands in pen.value:
        if operator == "moveTo":
            if contour is not None:
                raise ValueError("sample glyph contains an unfinished contour")
            contour = RecordingPen()
        if contour is None:
            raise ValueError(f"unexpected drawing operation: {operator}")

        getattr(contour, operator)(*operands)
        if operator in {"closePath", "endPath"}:
            bounds_pen = BoundsPen(None)
            contour.replay(bounds_pen)
            if bounds_pen.bounds is not None:
                bounds.append(tuple(float(value) for value in bounds_pen.bounds))
            contour = None

    if contour is not None:
        raise ValueError("sample glyph contains an unfinished contour")
    if not bounds:
        raise ValueError("sample glyph has no measurable contours")
    return bounds


def contains(outer: Bounds, inner: Bounds) -> bool:
    outer_x_min, outer_y_min, outer_x_max, outer_y_max = outer
    inner_x_min, inner_y_min, inner_x_max, inner_y_max = inner
    return (
        outer_x_min < inner_x_min
        and outer_y_min < inner_y_min
        and outer_x_max > inner_x_max
        and outer_y_max > inner_y_max
    )


def bounds_area(bounds: Bounds) -> float:
    x_min, y_min, x_max, y_max = bounds
    return (x_max - x_min) * (y_max - y_min)


def ring_stem_widths(glyph, glyph_set) -> tuple[float, float]:
    """Measure average left/right and top/bottom thicknesses of a ring."""
    bounds = contour_bounds(glyph, glyph_set)
    candidates = [
        (outer, inner)
        for outer in bounds
        for inner in bounds
        if outer is not inner and contains(outer, inner)
    ]
    if not candidates:
        raise ValueError("could not identify nested outer/inner ring contours")

    # Each Korean sample contains other strokes, but only the ieung has a
    # nested contour pair. Picking the largest candidate also makes the rule
    # robust if a source later gains a small enclosed detail elsewhere.
    outer, inner = max(candidates, key=lambda pair: bounds_area(pair[0]))
    outer_x_min, outer_y_min, outer_x_max, outer_y_max = outer
    inner_x_min, inner_y_min, inner_x_max, inner_y_max = inner

    vertical = fmean(
        (inner_x_min - outer_x_min, outer_x_max - inner_x_max)
    )
    horizontal = fmean(
        (inner_y_min - outer_y_min, outer_y_max - inner_y_max)
    )
    if vertical <= 0 or horizontal <= 0:
        raise ValueError("ring contours produced a non-positive stroke width")
    return vertical, horizontal


def ufo_stroke_scores(path: Path) -> tuple[float, float]:
    if not path.is_dir():
        raise FileNotFoundError(f"Astr master not found: {path}")

    font = Font.open(path, lazy=True)
    missing = [name for name in KOREAN_RING_GLYPHS if name not in font]
    if missing:
        raise ValueError(
            f"{path.name} is missing Korean samples: {', '.join(missing)}"
        )

    upm = float(font.info.unitsPerEm or 1000)
    samples = tuple(
        ring_stem_widths(font[name], font) for name in KOREAN_RING_GLYPHS
    )
    return (
        fmean(math.log(vertical / upm) for vertical, _horizontal in samples),
        fmean(math.log(horizontal / upm) for _vertical, horizontal in samples),
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
        missing = [
            character
            for character in INTER_RING_CHARACTERS
            if ord(character) not in cmap
        ]
        if missing:
            raise ValueError(
                f"Inter font is missing sample characters: {''.join(missing)}"
            )
        self.ring_glyph_names = tuple(
            cmap[ord(character)] for character in INTER_RING_CHARACTERS
        )

    @lru_cache(maxsize=None)
    def scores(self, weight: float, optical_size: int) -> tuple[float, float]:
        glyph_set = self.font.getGlyphSet(
            location={"wght": weight, "opsz": optical_size}
        )
        samples = tuple(
            ring_stem_widths(glyph_set[name], glyph_set)
            for name in self.ring_glyph_names
        )
        return (
            fmean(
                math.log(vertical / self.upm)
                for vertical, _horizontal in samples
            ),
            fmean(
                math.log(horizontal / self.upm)
                for _vertical, horizontal in samples
            ),
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

        estimate = max(
            self.minimum_weight,
            min(self.maximum_weight, (low + high) / 2),
        )
        precision = 10**OUTPUT_DECIMAL_PLACES
        # Keep the reported precision from rounding a matched stem
        # infinitesimally thinner than its Korean target.
        return math.ceil(estimate * precision) / precision

    def match_shared(
        self,
        text_scores: tuple[float, float],
        display_scores: tuple[float, float],
        scale: float,
    ) -> float:
        # Uniform Inter scaling multiplies both measured widths by this ratio.
        # Text and Display contribute equally to each directional match.
        scale_score = math.log(scale)
        target_vertical = fmean(
            (text_scores[0], display_scores[0])
        ) - scale_score
        target_horizontal = fmean(
            (text_scores[1], display_scores[1])
        ) - scale_score
        vertical_weight = self.find_weight(
            target_vertical,
            lambda weight: fmean(
                (
                    self.scores(weight, TEXT_OPSZ)[0],
                    self.scores(weight, DISPLAY_OPSZ)[0],
                )
            ),
        )
        horizontal_weight = self.find_weight(
            target_horizontal,
            lambda weight: fmean(
                (
                    self.scores(weight, TEXT_OPSZ)[1],
                    self.scores(weight, DISPLAY_OPSZ)[1],
                )
            ),
        )
        # When the Korean and Latin horizontal/vertical ratios differ, select
        # the heavier solution so Inter never resolves the mismatch by becoming
        # thinner in the other direction.
        return max(vertical_weight, horizontal_weight)


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
        ufo_stroke_scores(master_path(name, display=False))
        for name in MASTER_NAMES
    )
    display_targets = tuple(
        ufo_stroke_scores(master_path(name, display=True))
        for name in MASTER_NAMES
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
