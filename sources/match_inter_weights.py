from __future__ import annotations

import argparse
from collections.abc import Callable
from functools import lru_cache
import math
from pathlib import Path
from statistics import fmean

from fontTools.pens.areaPen import AreaPen
from fontTools.pens.perimeterPen import PerimeterPen
from fontTools.ttLib import TTFont
from ufoLib2 import Font


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INTER_FONT = (
    ROOT / "sources" / "vendor" / "inter" / "docs" / "font-files" / "InterVariable.ttf"
)
MASTER_NAMES = ("ExtraLight", "Light", "Regular", "Text", "Medium", "SemiBold")

# Simple Korean strokes whose measured area-to-perimeter ratio is stable.
KOREAN_SAMPLE_GLYPHS = ("iCompa-ko", "euCompa-ko", "mieumCompa-ko", "ieungCompa-ko")

# A comparable mixture of straight, round, uppercase, lowercase, and counter-bearing forms.
INTER_SAMPLE_CHARACTERS = "HInoOSB8"
TEXT_OPSZ = 14
DISPLAY_OPSZ = 32


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


def average_stroke(glyph, glyph_set) -> float:
    area_pen = AreaPen(glyph_set)
    glyph.draw(area_pen)
    perimeter_pen = PerimeterPen(glyph_set, tolerance=0.01)
    glyph.draw(perimeter_pen)
    if not perimeter_pen.value:
        raise ValueError("sample glyph has no measurable outline")

    # For a long rectangular stem, 2A/P approaches its thickness.
    return 2 * abs(area_pen.value) / perimeter_pen.value


def ufo_stroke_score(path: Path) -> float:
    if not path.is_dir():
        raise FileNotFoundError(f"Aster master not found: {path}")

    font = Font.open(path, lazy=True)
    missing = [name for name in KOREAN_SAMPLE_GLYPHS if name not in font]
    if missing:
        raise ValueError(f"{path.name} is missing Korean samples: {', '.join(missing)}")

    upm = float(font.info.unitsPerEm or 1000)
    return fmean(
        math.log(average_stroke(font[name], font) / upm)
        for name in KOREAN_SAMPLE_GLYPHS
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
        missing = [character for character in INTER_SAMPLE_CHARACTERS if ord(character) not in cmap]
        if missing:
            raise ValueError(f"Inter font is missing sample characters: {''.join(missing)}")
        self.glyph_names = tuple(cmap[ord(character)] for character in INTER_SAMPLE_CHARACTERS)

    @lru_cache(maxsize=None)
    def score(self, weight: float, optical_size: int) -> float:
        glyph_set = self.font.getGlyphSet(location={"wght": weight, "opsz": optical_size})
        return fmean(
            math.log(average_stroke(glyph_set[name], glyph_set) / self.upm)
            for name in self.glyph_names
        )

    def find_weight(self, target_score: float, candidate_score: Callable[[float], float]) -> int:
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
        candidates = {
            max(int(self.minimum_weight), min(int(self.maximum_weight), math.floor(estimate))),
            max(int(self.minimum_weight), min(int(self.maximum_weight), math.ceil(estimate))),
        }
        return min(
            candidates,
            key=lambda weight: abs(candidate_score(float(weight)) - target_score),
        )

    def match(self, korean_score: float, scale: float, optical_size: int) -> int:
        # Uniform Inter scaling multiplies its measured stroke width by the same ratio.
        target_score = korean_score - math.log(scale)
        return self.find_weight(
            target_score,
            lambda weight: self.score(weight, optical_size),
        )

    def match_shared(self, text_score: float, display_score: float, scale: float) -> int:
        target_score = fmean((text_score, display_score)) - math.log(scale)
        return self.find_weight(
            target_score,
            lambda weight: fmean(
                (self.score(weight, TEXT_OPSZ), self.score(weight, DISPLAY_OPSZ))
            ),
        )


def master_path(master_name: str, display: bool) -> Path:
    prefix = "Aster-Display" if display else "Aster-"
    return ROOT / "sources" / "masters" / f"{prefix}{master_name}.ufo"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Match Inter weights to measured Aster Korean master stroke widths."
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
    text_targets = tuple(
        ufo_stroke_score(master_path(name, display=False)) for name in MASTER_NAMES
    )
    display_targets = tuple(
        ufo_stroke_score(master_path(name, display=True)) for name in MASTER_NAMES
    )

    text_weights = tuple(
        model.match(target, args.scale, TEXT_OPSZ) for target in text_targets
    )
    display_weights = tuple(
        model.match(target, args.scale, DISPLAY_OPSZ) for target in display_targets
    )
    shared_weights = tuple(
        model.match_shared(text_target, display_target, args.scale)
        for text_target, display_target in zip(text_targets, display_targets)
    )

    print(f"Inter scale: {args.scale * 100:g}%")
    print(f"TEXT_INTER_WEIGHTS = {text_weights}")
    print(f"DISPLAY_INTER_WEIGHTS = {display_weights}")
    print(f"SHARED_INTER_WEIGHTS = {shared_weights}")


if __name__ == "__main__":
    main()
