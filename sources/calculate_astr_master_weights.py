from __future__ import annotations

import argparse
import math
from pathlib import Path
from statistics import fmean

from fontTools.pens.boundsPen import BoundsPen
from glyphsLib.classes import GSFont
from glyphsLib.parser import Parser
from openstep_plist import load as load_openstep


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "sources" / "AstaSans.glyphspackage"
MASTER_COUNT = 6
VERTICAL_STEM_GLYPH = "iCompa-ko"
HORIZONTAL_STEM_GLYPH = "euCompa-ko"
SAMPLE_GLYPH_FILES = {
    VERTICAL_STEM_GLYPH: "iC_ompa-ko.glyph",
    HORIZONTAL_STEM_GLYPH: "euC_ompa-ko.glyph",
}
OUTPUT_DECIMAL_PLACES = 3


def parse_weight(value: str) -> float:
    try:
        weight = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid weight: {value!r}") from error
    if not math.isfinite(weight):
        raise argparse.ArgumentTypeError("weight must be finite")
    return weight


def layer_stem_width(layer, orientation: str) -> float:
    pen = BoundsPen(None)
    layer.draw(pen)
    if pen.bounds is None:
        raise ValueError(f"{layer.name!r} has no measurable outline")

    x_min, y_min, x_max, y_max = pen.bounds
    if orientation == "vertical":
        return x_max - x_min
    if orientation == "horizontal":
        return y_max - y_min
    raise ValueError(f"unsupported stem orientation: {orientation}")


def master_axis_position(master) -> float:
    if not master.axes:
        raise ValueError(f"master {master.name!r} has no weight-axis coordinate")
    return float(master.axes[0])


def load_stem_samples(source: Path) -> GSFont:
    info_path = source / "fontinfo.plist"
    if not info_path.is_file():
        raise FileNotFoundError(f"font info not found: {info_path}")

    with info_path.open(encoding="utf-8") as file:
        data = load_openstep(file, use_numbers=True)

    data["glyphs"] = []
    for expected_name, filename in SAMPLE_GLYPH_FILES.items():
        glyph_path = source / "glyphs" / filename
        if not glyph_path.is_file():
            raise FileNotFoundError(f"stem sample not found: {glyph_path}")
        with glyph_path.open(encoding="utf-8") as file:
            glyph_data = load_openstep(file, use_numbers=True)
        if glyph_data.get("glyphname") != expected_name:
            raise ValueError(
                f"expected {expected_name!r} in {glyph_path}, "
                f"got {glyph_data.get('glyphname')!r}"
            )
        data["glyphs"].append(glyph_data)

    # Loading only the font info and the two sample glyphs keeps this utility
    # fast even though Asta Sans contains thousands of Hangul glyph files.
    font = GSFont()
    Parser(current_type=GSFont).parse_into_object(font, data)
    return font


def master_stem_widths(source: Path) -> tuple[list[float], list[float]]:
    if not source.is_dir():
        raise FileNotFoundError(f"Glyphs package not found: {source}")

    font = load_stem_samples(source)
    masters = sorted(font.masters, key=master_axis_position)
    if len(masters) != MASTER_COUNT:
        raise ValueError(
            f"expected {MASTER_COUNT} Asta Sans masters, got {len(masters)}"
        )

    glyphs = {
        VERTICAL_STEM_GLYPH: font.glyphs[VERTICAL_STEM_GLYPH],
        HORIZONTAL_STEM_GLYPH: font.glyphs[HORIZONTAL_STEM_GLYPH],
    }
    missing = [name for name, glyph in glyphs.items() if glyph is None]
    if missing:
        raise ValueError(f"missing stem samples: {', '.join(missing)}")

    vertical_widths: list[float] = []
    horizontal_widths: list[float] = []
    for master in masters:
        vertical_widths.append(
            layer_stem_width(
                glyphs[VERTICAL_STEM_GLYPH].layers[master.id], "vertical"
            )
        )
        horizontal_widths.append(
            layer_stem_width(
                glyphs[HORIZONTAL_STEM_GLYPH].layers[master.id], "horizontal"
            )
        )

    return vertical_widths, horizontal_widths


def normalized_progression(values: list[float], label: str) -> list[float]:
    if any(left >= right for left, right in zip(values, values[1:])):
        raise ValueError(
            f"{label} stem widths must increase across the masters: {values}"
        )

    span = values[-1] - values[0]
    return [(value - values[0]) / span for value in values]


def calculate_weights(
    source: Path, minimum_weight: float, maximum_weight: float
) -> tuple[float, ...]:
    if minimum_weight >= maximum_weight:
        raise ValueError("minimum weight must be less than maximum weight")

    vertical_widths, horizontal_widths = master_stem_widths(source)
    vertical_progression = normalized_progression(vertical_widths, "vertical")
    horizontal_progression = normalized_progression(
        horizontal_widths, "horizontal"
    )

    # Normalize the two directions independently so neither one dominates just
    # because its absolute stems or total growth happen to be larger. Their
    # equal-weight mean describes each master's position between the two fixed
    # endpoint weights.
    progression = [
        fmean((vertical, horizontal))
        for vertical, horizontal in zip(
            vertical_progression, horizontal_progression
        )
    ]
    weight_span = maximum_weight - minimum_weight
    return tuple(
        minimum_weight + position * weight_span for position in progression
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Place the six Astr masters between fixed minimum and maximum "
            "weights using Asta Sans's straight-stem progression."
        )
    )
    parser.add_argument("minimum_weight", type=parse_weight)
    parser.add_argument("maximum_weight", type=parse_weight)
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help=f"Asta Sans Glyphs package (default: {DEFAULT_SOURCE})",
    )
    args = parser.parse_args()

    weights = calculate_weights(
        args.source.resolve(), args.minimum_weight, args.maximum_weight
    )
    formatted = ",".join(
        f"{weight:.{OUTPUT_DECIMAL_PLACES}f}" for weight in weights
    )
    print(f"ASTR_MASTER_WEIGHTS={formatted}")


if __name__ == "__main__":
    main()
