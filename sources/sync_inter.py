from __future__ import annotations

import argparse
from collections import OrderedDict
from copy import deepcopy
from io import BytesIO, StringIO
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET

from fontTools.designspaceLib import (
    AxisDescriptor,
    DesignSpaceDocument,
    InstanceDescriptor,
    SourceDescriptor,
)
from fontTools.feaLib import ast
from fontTools.feaLib.parser import Parser
from fontTools.misc.filenames import userNameToFileName
import glyphsLib
from glyphsLib.classes import (
    GSAxis,
    GSClass,
    GSFeature,
    GSFeaturePrefix,
    GSFontInfoValue,
)
from glyphsLib.types import Point
from glyphsLib.writer import Writer
import openstep_plist
from ufoLib2 import Font
from ufomerge.scaler import scale_ufo


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FONT_SOURCE = ROOT / "sources" / "Astr.glyphspackage"
ASTR_DESIGNSPACE = ROOT / "sources" / "Astr.designspace"
INTER_CACHE = ROOT / "sources" / "vendor" / "inter"
INTER_SOURCE_IN_REPOSITORY = Path("src/Inter-Roman.glyphspackage")
DEFAULT_REPOSITORY = "https://github.com/rsms/inter.git"

FAMILY_NAME = "Astr"
FONT_METADATA = (
    (
        "copyrights",
        "Copyright 2026 Lee Minseo (quiple@quiple.dev); Copyright 2024 The "
        "Asta Sans Project Authors (https://github.com/42dot/Asta-Sans).",
        True,
    ),
    ("designers", "quiple; 42dot", True),
    ("designerURL", "https://quiple.dev; https://42dot.ai", False),
    (
        "licenses",
        "This Font Software is licensed under the SIL Open Font License, "
        "Version 1.1. This license is available with a FAQ at: "
        "https://openfontlicense.org",
        True,
    ),
    ("licenseURL", "https://openfontlicense.org", False),
    ("manufacturers", "quiple; 42dot", True),
    ("manufacturerURL", "https://quiple.dev; https://42dot.ai", False),
    ("vendorID", "QPLE", False),
)
DEFAULT_SCALE = 1.0
DEFAULT_ASTR_BASELINE = 0.0
BASELINE_REFERENCE_UPM = 1000.0
DEFAULT_MASTER_WEIGHTS = (225, 325, 400, 425, 475, 550)
DEFAULT_EXPORT_WEIGHTS = (225, 300, 400, 500, 550)
PUBLIC_EXPORT_WEIGHTS = (200, 300, 400, 500, 600)
TEXT_OPSZ = 14
DISPLAY_OPSZ = 32
DISPLAY_FAMILY = "Astr Display"
REGULAR_MASTER_ID = "54FF0D0B-6EB9-4889-908D-B8898FFCE7DE"
TEXT_MASTER_IDENTITIES = (
    ("m003", "ExtraLight"),
    ("m01", "Light"),
    (REGULAR_MASTER_ID, "Regular"),
    ("1E3F0FE2-7EB2-4B39-B562-A9E797F546FD", "Text"),
    ("E7059794-B319-4C6D-9648-9B840A1B2BBD", "Medium"),
    ("m005", "SemiBold"),
)
TEXT_INSTANCE_IDENTITIES = (
    ("ExtraLight", 200),
    ("Light", 300),
    ("Regular", "Regular"),
    ("Medium", 500),
    ("SemiBold", 600),
)
# Keep the original metadata namespace stable. Renaming these keys with the
# family would make an existing package look unsynchronized and force every
# imported glyph to be rewritten.
SYNC_STATE_KEY = "com.quiple.Astr.interSync"
IMPORTED_GLYPH_KEY = "com.quiple.Astr.interGlyph"
REMOVED_UNICODES_KEY = "com.quiple.Astr.interRemovedUnicodes"
DISPLAY_MASTER_NAMESPACE = uuid.UUID("899f9541-4354-42f3-9582-fdf66f401235")
SYNC_STATE_VERSION = 7
Weight = int | float
WEIGHT_DECIMAL_PLACES = 6
GEOMETRY_DECIMAL_PLACES = 6

# These Glyphs predicate tokens keep the Unicode ranges compact in the editable
# source. glyphsLib/Glyphs expands them only while compiling features.
HANGUL_COMPATIBILITY_JAMO_TOKEN = (
    '$[unicode matches "^(?:313(?:1|2|3|4|5|6|7|8|9|A|B|C|D|E|F)|'
    '31(?:4|5|6|7).|318(?:0|1|2|3|4|5|6|7|8|9|A|B|C|D|E))$"]'
)
HANGUL_SYLLABLE_TOKEN = (
    '$[unicode matches "^(?:A(?:C|D|E|F)..|B...|C...|'
    'D(?:0|1|2|3|4|5|6)..|D7(?:0|1|2|3|4|5|6|7|8|9).|'
    'D7A(?:0|1|2|3))$"]'
)
ASTR_CONTEXTUAL_UPPERCASE_CLASS = "ASTR_UC"
ASTR_CONTEXTUAL_LOWERCASE_CLASS = "ASTR_LC"


def run(command: list[str], cwd: Path | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


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


def _normalize_weight(value: int | float) -> Weight:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("weight must be a finite number")
    rounded = round(number, WEIGHT_DECIMAL_PLACES)
    return int(rounded) if rounded.is_integer() else rounded


def _format_number(value: int | float) -> str:
    return str(_normalize_weight(value))


def parse_weights(value: str) -> tuple[Weight, ...]:
    parts = [part for part in re.split(r"[\s,]+", value.strip()) if part]
    try:
        numbers = tuple(float(part) for part in parts)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "weights must be comma- or space-separated numbers"
        ) from error
    if any(not math.isfinite(number) for number in numbers):
        raise argparse.ArgumentTypeError("weights must be finite numbers")
    return tuple(_normalize_weight(number) for number in numbers)


def _validate_settings(
    scale: float,
    astr_baseline: float,
    master_weights: tuple[Weight, ...],
    export_weights: tuple[Weight, ...],
) -> None:
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("Inter scale must be greater than zero")
    if not math.isfinite(astr_baseline):
        raise ValueError("Astr baseline must be a finite number")
    if len(master_weights) != len(TEXT_MASTER_IDENTITIES):
        raise ValueError(
            f"Expected six master weights, got {len(master_weights)}"
        )
    if len(export_weights) != len(TEXT_INSTANCE_IDENTITIES):
        raise ValueError(
            f"Expected five export weights, got {len(export_weights)}"
        )
    if any(
        not math.isfinite(float(weight))
        for weight in (*master_weights, *export_weights)
    ):
        raise ValueError("Master and export weights must be finite numbers")
    if any(
        left >= right for left, right in zip(master_weights, master_weights[1:])
    ):
        raise ValueError("Master weights must be strictly increasing")
    if any(
        left >= right for left, right in zip(export_weights, export_weights[1:])
    ):
        raise ValueError("Export weights must be strictly increasing")
    if (
        export_weights[0] < master_weights[0]
        or export_weights[-1] > master_weights[-1]
    ):
        raise ValueError("Every export weight must fall inside the master range")
    # Regular may intentionally be an interpolated export between masters.
    # prepare_build_sources.py materializes it at both opsz endpoints so varLib
    # still has a real source at the variable font's public default (wght=400).


def _settings_dict(
    scale: float,
    astr_baseline: float,
    master_weights: tuple[Weight, ...],
    export_weights: tuple[Weight, ...],
) -> dict:
    master_weights = tuple(_normalize_weight(value) for value in master_weights)
    export_weights = tuple(_normalize_weight(value) for value in export_weights)
    _validate_settings(scale, astr_baseline, master_weights, export_weights)
    return {
        "scale": scale,
        "astrBaseline": astr_baseline,
        "masterWeights": list(master_weights),
        "exportWeights": list(export_weights),
    }


def _settings_from_state(state: dict) -> dict:
    saved = state.get("settings") or {}
    # Version 4 and 5 sources moved Inter by a signed `baseline` value. Moving
    # Astr by the opposite amount preserves the relative alignment while
    # restoring Inter to its native baseline.
    if "astrBaseline" in saved:
        astr_baseline = float(saved["astrBaseline"])
    else:
        astr_baseline = -float(saved.get("baseline", 0))
    return _settings_dict(
        float(saved.get("scale", DEFAULT_SCALE)),
        astr_baseline,
        tuple(
            _normalize_weight(value)
            for value in saved.get("masterWeights", DEFAULT_MASTER_WEIGHTS)
        ),
        tuple(
            _normalize_weight(value)
            for value in saved.get("exportWeights", DEFAULT_EXPORT_WEIGHTS)
        ),
    )


def _master_specs(
    master_weights: tuple[Weight, ...],
) -> tuple[tuple[str, str, Weight], ...]:
    return tuple(
        (master_id, name, weight)
        for (master_id, name), weight in zip(TEXT_MASTER_IDENTITIES, master_weights)
    )


def _instance_interpolations(
    export_weight: Weight, master_weights: tuple[Weight, ...]
) -> tuple[tuple[str, float], ...]:
    master_specs = _master_specs(master_weights)
    for master_id, _name, master_weight in master_specs:
        if export_weight == master_weight:
            return ((master_id, 1.0),)
    for left, right in zip(master_specs, master_specs[1:]):
        left_id, _left_name, left_weight = left
        right_id, _right_name, right_weight = right
        if left_weight < export_weight < right_weight:
            right_ratio = (export_weight - left_weight) / (
                right_weight - left_weight
            )
            return (
                (left_id, round(1 - right_ratio, 5)),
                (right_id, round(right_ratio, 5)),
            )
    raise ValueError(
        f"Export weight {export_weight} is outside the master range {master_weights}"
    )


def _instance_specs(
    master_weights: tuple[Weight, ...], export_weights: tuple[Weight, ...]
) -> tuple[tuple[str, Weight, int | str, tuple[tuple[str, float], ...]], ...]:
    return tuple(
        (
            name,
            weight,
            weight_class,
            _instance_interpolations(weight, master_weights),
        )
        for (name, weight_class), weight in zip(
            TEXT_INSTANCE_IDENTITIES, export_weights
        )
    )


def _weight_axis_mapping(export_weights: tuple[Weight, ...]) -> dict[str, Weight]:
    return {
        str(public_weight): design_weight
        for public_weight, design_weight in zip(
            PUBLIC_EXPORT_WEIGHTS, export_weights
        )
    }


def fetch_latest_inter(repository: str) -> tuple[Path, str]:
    INTER_CACHE.parent.mkdir(parents=True, exist_ok=True)
    if INTER_CACHE.exists() and not (INTER_CACHE / ".git").is_dir():
        shutil.rmtree(INTER_CACHE)

    if not INTER_CACHE.exists():
        run(["git", "clone", "--depth", "1", repository, str(INTER_CACHE)])
    else:
        run(["git", "-C", str(INTER_CACHE), "remote", "set-url", "origin", repository])
        run(["git", "-C", str(INTER_CACHE), "fetch", "--depth", "1", "origin", "HEAD"])
        run(["git", "-C", str(INTER_CACHE), "reset", "--hard", "FETCH_HEAD"])
        run(["git", "-C", str(INTER_CACHE), "clean", "-ffd"])

    source = INTER_CACHE / INTER_SOURCE_IN_REPOSITORY
    if not source.is_dir():
        raise FileNotFoundError(f"Inter source not found after fetch: {source}")
    commit = subprocess.check_output(
        ["git", "-C", str(INTER_CACHE), "rev-parse", "HEAD"], text=True
    ).strip()
    return source, commit


def _instance_name(weight: Weight, optical_size: int) -> str:
    return f"Inter sync w{_format_number(weight)} o{optical_size}"


def _instance_filename(weight: Weight, optical_size: int) -> str:
    return f"Inter-w{_format_number(weight)}-o{optical_size}.ufo"


def _scale_number(value: int | float | None, factor: float) -> int | None:
    if value is None:
        return None
    return round(value * factor)


def scale_feature_geometry(
    node: object,
    factor: float,
    seen: set[int] | None = None,
) -> None:
    if seen is None:
        seen = set()
    node_id = id(node)
    if node_id in seen:
        return
    seen.add(node_id)

    if isinstance(node, ast.ValueRecord):
        for name in ("xPlacement", "yPlacement", "xAdvance", "yAdvance"):
            setattr(node, name, _scale_number(getattr(node, name), factor))
        return
    if isinstance(node, ast.Anchor):
        if node.name is None:
            node.x = _scale_number(node.x, factor)
            node.y = _scale_number(node.y, factor)
        return
    if isinstance(node, dict):
        values = node.values()
    elif isinstance(node, (list, tuple, set)):
        values = node
    elif isinstance(node, ast.Element):
        values = vars(node).values()
    else:
        return
    for value in values:
        scale_feature_geometry(value, factor, seen)


def _tool(name: str) -> str:
    path = Path(sys.executable).with_name(name)
    if not path.is_file():
        raise FileNotFoundError(
            f"{name} was not found next to {sys.executable}; run `make setup` first"
        )
    return str(path)


def prepare_inter_font(
    inter_source: Path,
    work: Path,
    astr_upm: float,
    settings: dict,
):
    scale = float(settings["scale"])
    master_weights = tuple(settings["masterWeights"])
    masters = work / "masters"
    instances = work / "instances"
    masters.mkdir(parents=True)
    instances.mkdir(parents=True)
    original_designspace = masters / "Inter-Roman.designspace"
    instance_designspace = masters / "Inter-Astr-Sync.designspace"

    run(
        [
            _tool("glyphs2ufo"),
            "--minimal",
            "--expand-includes",
            "-m",
            str(masters),
            "-d",
            str(original_designspace),
            "-n",
            str(instances),
            str(inter_source),
        ]
    )

    document = DesignSpaceDocument.fromfile(original_designspace)
    source_weight_axis = next(
        (axis for axis in document.axes if axis.tag == "wght"), None
    )
    if source_weight_axis is None:
        raise ValueError("Inter source has no wght axis")
    unsupported_weights = [
        weight
        for weight in master_weights
        if not source_weight_axis.minimum <= weight <= source_weight_axis.maximum
    ]
    if unsupported_weights:
        raise ValueError(
            "Inter master weights must be inside the source's "
            f"{source_weight_axis.minimum:g}–{source_weight_axis.maximum:g} range: "
            + ", ".join(str(weight) for weight in unsupported_weights)
        )
    document.instances = []
    for optical_size in (TEXT_OPSZ, DISPLAY_OPSZ):
        for weight in master_weights:
            instance = InstanceDescriptor()
            instance.name = _instance_name(weight, optical_size)
            instance.familyName = "Inter"
            instance.styleName = f"Sync w{weight} o{optical_size}"
            instance.filename = str(
                Path("..") / "instances" / _instance_filename(weight, optical_size)
            )
            instance.designLocation = {
                "Optical size": optical_size,
                "Weight": weight,
            }
            document.addInstance(instance)
    document.write(instance_designspace)

    run(
        [
            _tool("fontmake"),
            "-m",
            str(instance_designspace),
            "-o",
            "ufo",
            "-i",
            "Inter sync.*",
            "--expand-features-to-instances",
        ]
    )

    as_masters = DesignSpaceDocument()
    weight_axis = AxisDescriptor()
    weight_axis.name = "Weight"
    weight_axis.tag = "wght"
    weight_axis.minimum = min(master_weights)
    weight_axis.default = master_weights[2]
    weight_axis.maximum = max(master_weights)
    as_masters.addAxis(weight_axis)
    optical_axis = AxisDescriptor()
    optical_axis.name = "Optical size"
    optical_axis.tag = "opsz"
    optical_axis.minimum = TEXT_OPSZ
    optical_axis.default = TEXT_OPSZ
    optical_axis.maximum = DISPLAY_OPSZ
    as_masters.addAxis(optical_axis)

    for optical_size in (TEXT_OPSZ, DISPLAY_OPSZ):
        for weight in master_weights:
            path = instances / _instance_filename(weight, optical_size)
            ufo = Font.open(path)
            factor = astr_upm / float(ufo.info.unitsPerEm) * scale
            feature_file = Parser(
                StringIO(ufo.features.text),
                glyphNames=set(ufo.keys()),
                includeDir=masters,
            ).parse()
            scale_feature_geometry(feature_file, factor)
            scale_ufo(ufo, factor)
            for glyph in ufo:
                if glyph.verticalOrigin is not None:
                    glyph.verticalOrigin *= factor
            # The extra visual scale must not change Astr's UPM.
            ufo.info.unitsPerEm = astr_upm
            ufo.features.text = feature_file.asFea()
            ufo.save(path, overwrite=True)

            source = SourceDescriptor()
            source.path = str(path)
            source.name = _instance_name(weight, optical_size)
            source.familyName = ufo.info.familyName
            source.styleName = ufo.info.styleName
            source.designLocation = {
                "Weight": weight,
                "Optical size": optical_size,
            }
            if (weight, optical_size) == (master_weights[2], TEXT_OPSZ):
                source.copyLib = True
                source.copyGroups = True
                source.copyFeatures = True
                source.copyInfo = True
            as_masters.addSource(source)

    converted_designspace = work / "Inter-Astr-Masters.designspace"
    as_masters.write(converted_designspace)
    inter_font = glyphsLib.to_glyphs(as_masters, minimize_ufo_diffs=False)
    for master in inter_font.masters:
        master.axes = list(master.axes[:2])
    return inter_font


def _display_master_id(text_master_id: str) -> str:
    return str(uuid.uuid5(DISPLAY_MASTER_NAMESPACE, text_master_id)).upper()


def _ensure_axis_mappings(font, export_weights: tuple[Weight, ...]) -> None:
    mappings = deepcopy(font.customParameters["Axis Mappings"] or {})
    mappings["wght"] = _weight_axis_mapping(export_weights)
    mappings["opsz"] = {
        str(TEXT_OPSZ): TEXT_OPSZ,
        str(DISPLAY_OPSZ): DISPLAY_OPSZ,
    }
    font.customParameters["Axis Mappings"] = mappings


def _set_astr_metadata(font) -> None:
    metadata_keys = {key for key, _value, _localized in FONT_METADATA}
    properties = [
        property_value
        for property_value in font.properties
        if property_value.key not in metadata_keys
    ]
    for key, value, localized in FONT_METADATA:
        property_value = GSFontInfoValue(key, value)
        if localized:
            property_value._localized_values = {"dflt": value}
        properties.append(property_value)
    font.properties = properties


def configure_astr_project(font, settings: dict) -> None:
    """Turn the original Asta Sans project structure into Astr in place."""
    master_weights = tuple(settings["masterWeights"])
    export_weights = tuple(settings["exportWeights"])
    text_master_specs = _master_specs(master_weights)
    text_instance_specs = _instance_specs(master_weights, export_weights)
    axis_tags = [axis.axisTag for axis in font.axes]
    if axis_tags not in (["wght"], ["wght", "opsz"]):
        raise ValueError(
            "Expected an Asta Sans project with a wght axis, optionally followed "
            f"by opsz; got {axis_tags}"
        )

    masters_by_id = {master.id: master for master in font.masters}
    missing = [
        master_id
        for master_id, _name, _weight in text_master_specs
        if master_id not in masters_by_id
    ]
    if missing:
        raise ValueError(
            "The package does not have the expected six Asta Sans masters: "
            + ", ".join(missing)
        )

    has_opsz = axis_tags == ["wght", "opsz"]
    text_master_ids = {
        master_id for master_id, _name, _weight in text_master_specs
    }
    for master_id, name, weight in text_master_specs:
        master = masters_by_id[master_id]
        master.name = name
        master.axes = [weight, TEXT_OPSZ] if has_opsz else [weight]

        if has_opsz:
            display_id = _display_master_id(master_id)
            display = masters_by_id.get(display_id)
            if display is None:
                raise ValueError(
                    f"The existing opsz project is missing Display master {display_id}"
                )
            display.name = f"Display {name}"
            display.axes = [weight, DISPLAY_OPSZ]

    # Rebuild the export list from the first five Text exports.  This also
    # removes Asta Sans's obsolete sixth export and any old Display copies;
    # _ensure_display_instances recreates the five Display exports below.
    text_instances = [
        instance
        for instance in font.instances
        if not has_opsz
        or len(instance.axes) < 2
        or int(round(instance.axes[1])) == TEXT_OPSZ
    ]
    if len(text_instances) < len(text_instance_specs):
        raise ValueError(
            "Expected at least five Asta Sans Text export instances, got "
            f"{len(text_instances)}"
        )
    text_instances = text_instances[: len(text_instance_specs)]
    for instance, (name, weight, weight_class, interpolations) in zip(
        text_instances, text_instance_specs
    ):
        instance.name = name
        instance.axes = [weight, TEXT_OPSZ] if has_opsz else [weight]
        instance.weight = weight_class
        instance.instanceInterpolations = OrderedDict(interpolations)
        instance.active = True
        instance.properties = []
        if instance.customParameters["Has WWS Names"] is not None:
            del instance.customParameters["Has WWS Names"]
    font.instances = text_instances

    font.familyName = FAMILY_NAME
    _set_astr_metadata(font)
    font.customParameters["Variable Font Origin"] = REGULAR_MASTER_ID
    _ensure_axis_mappings(font, export_weights)

    # When this is run against an already converted package, discard only
    # unexpected masters.  The six Text and six deterministic Display masters
    # remain exactly the same, making the command idempotent.
    if has_opsz:
        expected_ids = text_master_ids | {
            _display_master_id(master_id) for master_id in text_master_ids
        }
        font.masters = [
            master for master in font.masters if master.id in expected_ids
        ]


def _preserve_imported_component_positions(inter_font) -> None:
    # The imported Inter layers come from instantiated UFOs, so their component
    # transforms are final geometry rather than Glyphs automatic-alignment
    # instructions.  If automatic alignment remains enabled, Glyphs recalculates
    # component-only glyphs when the package is opened or saved and can discard
    # intentional offsets (for example the lower circle in _zero_percent1).
    for glyph in inter_font.glyphs:
        for layer in glyph.layers:
            for component in layer.components:
                component.alignment = -1


def _detached_deepcopy(item, parent_attribute: str):
    parent = getattr(item, parent_attribute)
    memo = {id(parent): None} if parent is not None else {}
    if parent is not None:
        grandparent = getattr(parent, "parent", None) or getattr(parent, "font", None)
        if grandparent is not None:
            memo[id(grandparent)] = None
    copied = deepcopy(item, memo)
    setattr(copied, parent_attribute, None)
    return copied


def _master_records(
    font, state: dict, master_weights: tuple[Weight, ...]
) -> tuple[list[dict], list[dict], set[str]]:
    text_records = state.get("textMasters")
    display_records = state.get("displayMasters")
    if text_records and display_records:
        previous_weights = {
            record["id"]: _normalize_weight(record["weight"])
            for record in [*text_records, *display_records]
        }
        text_records = [
            {
                "weight": _normalize_weight(
                    font.masters[record["id"]].axes[0]
                ),
                "id": record["id"],
            }
            for record in text_records
        ]
        display_records = [
            {
                "weight": _normalize_weight(
                    font.masters[record["id"]].axes[0]
                ),
                "id": record["id"],
            }
            for record in display_records
        ]
        text_coordinates = tuple(record["weight"] for record in text_records)
        display_coordinates = tuple(
            record["weight"] for record in display_records
        )
        if (
            text_coordinates != master_weights
            or display_coordinates != master_weights
        ):
            raise ValueError(
                "Expected the Text and Display master weights "
                f"{master_weights}, got {text_coordinates} and {display_coordinates}"
            )
        state["textMasters"] = text_records
        state["displayMasters"] = display_records
        changed_master_ids = {
            record["id"]
            for record in [*text_records, *display_records]
            if previous_weights[record["id"]] != record["weight"]
        }
        return text_records, display_records, changed_master_ids

    if [axis.axisTag for axis in font.axes] != ["wght"]:
        raise ValueError(
            "The first Inter sync expects the original one-axis Astr project"
        )
    masters = list(font.masters)
    coordinates = [_normalize_weight(master.axes[0]) for master in masters]
    if tuple(coordinates) != master_weights:
        raise ValueError(
            "Expected the six Astr master weights "
            f"{master_weights}, got {coordinates}"
        )

    font.axes.append(GSAxis("Optical size", "opsz"))
    text_records = []
    display_records = []
    for master in masters:
        weight = _normalize_weight(master.axes[0])
        master.axes = [weight, TEXT_OPSZ]
        text_records.append({"weight": weight, "id": master.id})

        display = _detached_deepcopy(master, "font")
        display.id = _display_master_id(master.id)
        display.name = f"Display {master.name}"
        display.axes = [weight, DISPLAY_OPSZ]
        # FontMasterProxy.append also creates an empty layer in every glyph.
        # The mirrored layers below replace those, so append directly here.
        display.font = font
        font._masters.append(display)
        display_records.append({"weight": weight, "id": display.id})

    for instance in font.instances:
        instance.axes = [instance.axes[0], TEXT_OPSZ]
    state["textMasters"] = text_records
    state["displayMasters"] = display_records
    return text_records, display_records, set()


def _remove_previous_inter_glyphs(font, state: dict) -> None:
    imported = set(state.get("importedGlyphs", []))
    retained = [
        glyph
        for glyph in font.glyphs
        if glyph.name not in imported and not glyph.userData.get(IMPORTED_GLYPH_KEY)
    ]
    font.glyphs.setter(retained)
    for glyph in font.glyphs:
        removed = glyph.userData.get(REMOVED_UNICODES_KEY)
        if removed:
            glyph.unicodes = list(dict.fromkeys([*glyph.unicodes, *removed]))
            del glyph.userData[REMOVED_UNICODES_KEY]


def _normalize_geometry(value: int | float) -> int | float:
    rounded = round(float(value), GEOMETRY_DECIMAL_PLACES)
    return int(rounded) if rounded.is_integer() else rounded


def _shift_position(item, delta_x: float, delta_y: float) -> None:
    position = item.position
    item.position = Point(
        _normalize_geometry(position[0] + delta_x),
        _normalize_geometry(position[1] + delta_y),
    )


def _component_baseline_compensation(
    component,
    baseline: float,
    base_is_inter: bool,
) -> tuple[float, float]:
    if base_is_inter:
        return 0, baseline
    _xx, _xy, yx, yy, _dx, _dy = component.transform
    return -yx * baseline, baseline - yy * baseline


def _shift_astr_layer(
    layer,
    previous_baseline: float,
    target_baseline: float,
    previous_inter_names: set[str],
    inter_names: set[str],
) -> None:
    delta = target_baseline - previous_baseline
    for path in layer.paths:
        for node in path.nodes:
            _shift_position(node, 0, delta)
    for anchor in layer.anchors:
        _shift_position(anchor, 0, delta)
    for guide in layer.guides:
        _shift_position(guide, 0, delta)

    for component in layer.components:
        previous_compensation = _component_baseline_compensation(
            component,
            previous_baseline,
            component.name in previous_inter_names,
        )
        target_compensation = _component_baseline_compensation(
            component,
            target_baseline,
            component.name in inter_names,
        )
        _shift_position(
            component,
            target_compensation[0] - previous_compensation[0],
            target_compensation[1] - previous_compensation[1],
        )
        if component.name in inter_names and target_baseline:
            # The Inter base stays at its native baseline, so this Astr-owned
            # composite needs an explicit offset. Disable automatic alignment
            # so Glyphs does not discard it when opening or saving the package.
            component.alignment = -1

    if layer.hasBackground:
        _shift_astr_layer(
            layer.background,
            previous_baseline,
            target_baseline,
            previous_inter_names,
            inter_names,
        )


def _previous_astr_baseline(state: dict) -> float:
    saved = state.get("settings") or {}
    if "astrBaseline" not in saved:
        # Legacy sources moved Inter instead, so no Astr baseline shift has
        # been applied even when their saved `baseline` value is non-zero.
        return 0.0
    return float(saved["astrBaseline"])


def _astr_baseline_in_font_units(baseline: float, upm: float) -> float:
    return baseline * upm / BASELINE_REFERENCE_UPM


def _shift_astr_owned_glyphs(
    font,
    state: dict,
    settings: dict,
    inter_names: set[str],
) -> int:
    previous = _astr_baseline_in_font_units(
        _previous_astr_baseline(state),
        float(font.upm),
    )
    target = _astr_baseline_in_font_units(
        float(settings["astrBaseline"]),
        float(font.upm),
    )
    previous_inter_names = set(state.get("importedGlyphs", []))
    baseline_changed = not math.isclose(
        previous,
        target,
        abs_tol=10**-GEOMETRY_DECIMAL_PLACES,
    )
    ownership_changed = previous_inter_names != inter_names
    if not baseline_changed and not ownership_changed:
        return 0

    if baseline_changed:
        print(
            "Moving Astr-owned glyphs by "
            f"{_normalize_geometry(target - previous):g} units "
            f"at UPM {font.upm}...",
            flush=True,
        )
    for glyph in font.glyphs:
        for layer in glyph.layers:
            _shift_astr_layer(
                layer,
                previous,
                target,
                previous_inter_names,
                inter_names,
            )
    return len(font.glyphs)


def _mirror_astr_layers(
    font, text_records: list[dict], display_records: list[dict]
) -> None:
    for glyph in font.glyphs:
        layers = list(glyph.layers)
        by_id = {layer.layerId: layer for layer in layers}
        added_layer = False
        for text, display in zip(text_records, display_records):
            if display["id"] in by_id:
                continue
            source = by_id.get(text["id"])
            if source is None:
                continue
            layer = _detached_deepcopy(source, "parent")
            layer.layerId = display["id"]
            layer.associatedMasterId = display["id"]
            layer.name = font.masters[display["id"]].name
            layers.append(layer)
            added_layer = True
        if added_layer:
            glyph.layers.setter(layers)

    for text, display in zip(text_records, display_records):
        if display["id"] not in font.kerning:
            font.kerning[display["id"]] = deepcopy(
                font.kerning.get(text["id"], OrderedDict())
            )


def _display_property(key: str, value: str, localized: bool = False):
    prop = GSFontInfoValue(key, value)
    if localized:
        prop._localized_values = {"dflt": value}
    return prop


def _ensure_display_instances(
    font, text_records: list[dict], display_records: list[dict]
) -> None:
    existing = {
        (
            instance.name,
            _normalize_weight(instance.axes[0]),
            int(round(instance.axes[1])),
        ): instance
        for instance in font.instances
        if len(instance.axes) >= 2
    }
    master_ids = {
        text["id"]: display["id"]
        for text, display in zip(text_records, display_records)
    }
    text_instances = [
        instance
        for instance in font.instances
        if len(instance.axes) >= 2
        and int(round(instance.axes[1])) == TEXT_OPSZ
        and instance.active
    ]
    for text_instance in text_instances:
        weight = _normalize_weight(text_instance.axes[0])
        key = (text_instance.name, weight, DISPLAY_OPSZ)
        display = existing.get(key)
        if display is None:
            display = _detached_deepcopy(text_instance, "parent")
            font.instances.append(display)
            existing[key] = display
        display.axes = [weight, DISPLAY_OPSZ]
        display.instanceInterpolations = OrderedDict(
            (master_ids.get(master_id, master_id), value)
            for master_id, value in text_instance.instanceInterpolations.items()
        )
        display.properties = [
            _display_property("familyNames", DISPLAY_FAMILY, localized=True),
            _display_property("WWSFamilyName", DISPLAY_FAMILY),
            _display_property("WWSSubfamilyName", display.name),
        ]
        display.customParameters["Has WWS Names"] = False


def _snapshot_layout_object(item) -> dict:
    return {
        "name": item.name,
        "code": item.code,
        "automatic": bool(item.automatic),
        "disabled": bool(item.disabled),
        "notes": item.notes or "",
        "labels": deepcopy(item.labels or []),
    }


def _layout_object(cls, snapshot: dict):
    item = cls(snapshot["name"], snapshot["code"])
    item.automatic = snapshot.get("automatic", False)
    item.disabled = snapshot.get("disabled", False)
    item.notes = snapshot.get("notes", "")
    item.labels = deepcopy(snapshot.get("labels", []))
    return item


def _remove_named(collection, names: set[str]) -> None:
    for item in list(collection):
        if item.name in names:
            collection.remove(item)


def _restore_layout(font, state: dict) -> None:
    specs = (
        (font.features, GSFeature, "interFeatures", "replacedFeatures"),
        (font.featurePrefixes, GSFeaturePrefix, "interPrefixes", "replacedPrefixes"),
        (font.classes, GSClass, "interClasses", "replacedClasses"),
    )
    for collection, cls, inter_key, replaced_key in specs:
        _remove_named(collection, set(state.get(inter_key, [])))
        for snapshot in state.get(replaced_key, {}).values():
            _remove_named(collection, {snapshot["name"]})
            collection.append(_layout_object(cls, snapshot))


LANGUAGE_SYSTEM_RE = re.compile(
    r"^\s*languagesystem\s+([A-Za-z0-9]{3,4})\s+([A-Za-z0-9]{3,4})\s*;",
    re.MULTILINE,
)


def _merge_language_systems(base_code: str, inter_code: str) -> str:
    systems: list[tuple[str, str]] = []
    for code in (base_code, inter_code):
        for match in LANGUAGE_SYSTEM_RE.finditer(code):
            system = (match.group(1), match.group(2))
            if system not in systems:
                systems.append(system)
    return "\n".join(f"languagesystem {script} {language};" for script, language in systems)


def _replace_layout_collection(
    collection,
    incoming,
    state: dict,
    inter_key: str,
    replaced_key: str,
    merge_languages: bool = False,
) -> None:
    originals = dict(state.get(replaced_key, {}))
    existing = {item.name: item for item in collection}
    incoming_names = []
    for item in incoming:
        incoming_names.append(item.name)
        previous = existing.get(item.name)
        if previous is not None and item.name not in originals:
            originals[item.name] = _snapshot_layout_object(previous)
        replacement = _layout_object(type(item), _snapshot_layout_object(item))
        if merge_languages and item.name == "Languagesystems" and previous is not None:
            replacement.code = _merge_language_systems(previous.code, replacement.code)
        _remove_named(collection, {item.name})
        collection.append(replacement)
    state[inter_key] = incoming_names
    state[replaced_key] = originals


def _replace_layout(font, inter_font, state: dict) -> None:
    _replace_layout_collection(
        font.features,
        inter_font.features,
        state,
        "interFeatures",
        "replacedFeatures",
    )
    _replace_layout_collection(
        font.featurePrefixes,
        inter_font.featurePrefixes,
        state,
        "interPrefixes",
        "replacedPrefixes",
        merge_languages=True,
    )
    _replace_layout_collection(
        font.classes,
        inter_font.classes,
        state,
        "interClasses",
        "replacedClasses",
    )


def _extend_inter_contextual_class(
    calt,
    source_name: str,
    target_name: str,
    predicate_token: str,
    comment: str,
) -> None:
    if re.search(rf"(?<![\w.])@{re.escape(target_name)}(?![\w.])", calt.code):
        return

    # Keep Inter's local class untouched, define a compact Astr union beside
    # it, and use the union in every later rule that referred to that class.
    match = re.search(
        rf"(?ms)^(?P<indent>[ \t]*)@{re.escape(source_name)}"
        r"\s*=\s*\[.*?\];[ \t]*$",
        calt.code,
    )
    if match is None:
        raise ValueError(
            f"Inter calt no longer defines the expected @{source_name} class"
        )

    indent = match.group("indent")
    astr_class = (
        f"\n{indent}# Astr: {comment}"
        f"\n{indent}@{target_name} = [@{source_name}"
        f"\n{indent}    {predicate_token}"
        f"\n{indent}];"
    )
    tail = re.sub(
        rf"(?<![\w.])@{re.escape(source_name)}(?![\w.])",
        f"@{target_name}",
        calt.code[match.end() :],
    )
    calt.code = calt.code[: match.end()] + astr_class + tail


def _extend_inter_contextual_case(font) -> None:
    """Map Hangul syllables and compatibility jamo to Inter calt casing."""
    calt = next((feature for feature in font.features if feature.name == "calt"), None)
    if calt is None:
        raise ValueError("Inter calt feature is missing")

    _extend_inter_contextual_class(
        calt,
        "UC",
        ASTR_CONTEXTUAL_UPPERCASE_CLASS,
        HANGUL_SYLLABLE_TOKEN,
        "Hangul syllables behave like capitals in contextual punctuation.",
    )
    _extend_inter_contextual_class(
        calt,
        "LC",
        ASTR_CONTEXTUAL_LOWERCASE_CLASS,
        HANGUL_COMPATIBILITY_JAMO_TOKEN,
        "Hangul compatibility jamo behave like lowercase in contextual punctuation.",
    )


def _kerning_group_keys(glyph) -> set[str]:
    keys: set[str] = set()
    for group in (glyph.leftKerningGroup, glyph.rightKerningGroup):
        if group:
            keys.add(f"@MMK_L_{group}")
            keys.add(f"@MMK_R_{group}")
    return keys


def _inter_kerning_keys(inter_font) -> set[str]:
    keys: set[str] = set()
    for master_kerning in inter_font.kerning.values():
        for left, rights in master_kerning.items():
            keys.add(left)
            keys.update(rights.keys())
    for glyph in inter_font.glyphs:
        keys.add(glyph.name)
        keys.add(glyph.id)
        keys.update(_kerning_group_keys(glyph))
    return keys


def _purge_kerning(
    font, keys: set[str], master_ids: set[str] | None = None
) -> None:
    for master_id, master_kerning in list(font.kerning.items()):
        if master_ids is not None and master_id not in master_ids:
            continue
        cleaned = OrderedDict()
        for left, rights in master_kerning.items():
            if left in keys:
                continue
            remaining = OrderedDict(
                (right, value) for right, value in rights.items() if right not in keys
            )
            if remaining:
                cleaned[left] = remaining
        font.kerning[master_id] = cleaned


def _merge_kerning(
    font,
    inter_font,
    master_map: dict[str, str],
    target_master_ids: set[str] | None = None,
) -> None:
    for inter_master_id, master_kerning in inter_font.kerning.items():
        target_id = master_map[inter_master_id]
        if target_master_ids is not None and target_id not in target_master_ids:
            continue
        target = font.kerning.setdefault(target_id, OrderedDict())
        for left, rights in master_kerning.items():
            target.setdefault(left, OrderedDict()).update(deepcopy(rights))


def _remap_inter_glyph(
    glyph, master_map: dict[str, str], font, existing_markers: dict[str, object]
):
    copied = _detached_deepcopy(glyph, "parent")
    layers = []
    for layer in copied.layers:
        target_id = master_map.get(layer.associatedMasterId)
        if target_id is None:
            continue
        layer.layerId = target_id
        layer.associatedMasterId = target_id
        layer.name = font.masters[target_id].name
        layers.append(layer)
    copied.layers.setter(layers)
    # Preserve the existing stable marker. Old packages used the repository
    # commit here; changing that value alone would touch every imported file.
    copied.userData[IMPORTED_GLYPH_KEY] = existing_markers.get(glyph.name, True)
    return copied


def _replace_master_layers(
    font,
    inter_font,
    master_map: dict[str, str],
    target_master_ids: set[str],
) -> None:
    for inter_glyph in inter_font.glyphs:
        target_glyph = font.glyphs[inter_glyph.name]
        replacements = {}
        for layer in inter_glyph.layers:
            target_id = master_map.get(layer.associatedMasterId)
            if target_id not in target_master_ids:
                continue
            replacement = _detached_deepcopy(layer, "parent")
            replacement.layerId = target_id
            replacement.associatedMasterId = target_id
            replacement.name = font.masters[target_id].name
            replacements[target_id] = replacement

        existing_ids = {layer.layerId for layer in target_glyph.layers}
        missing = target_master_ids - existing_ids
        if missing:
            raise ValueError(
                f"Imported glyph {inter_glyph.name} is missing target layers "
                f"{sorted(missing)}"
            )
        target_glyph.layers.setter(
            [
                replacements.get(layer.layerId, layer)
                for layer in target_glyph.layers
            ]
        )


def _merge_glyphs(
    font,
    inter_font,
    master_map: dict[str, str],
    state: dict,
    existing_markers: dict[str, object],
) -> tuple[int, int]:
    base_glyphs = list(font.glyphs)
    base_by_name = {glyph.name: glyph for glyph in base_glyphs}
    inter_by_name = {
        glyph.name: _remap_inter_glyph(
            glyph, master_map, font, existing_markers
        )
        for glyph in inter_font.glyphs
    }
    overlap_names = set(base_by_name) & set(inter_by_name)

    inter_unicodes = {
        unicode_value
        for glyph in inter_by_name.values()
        for unicode_value in glyph.unicodes
    }
    removed_unicodes = 0
    for glyph in base_glyphs:
        if glyph.name in overlap_names:
            continue
        conflicts = [value for value in glyph.unicodes if value in inter_unicodes]
        if conflicts:
            glyph.userData[REMOVED_UNICODES_KEY] = conflicts
            glyph.unicodes = [
                value for value in glyph.unicodes if value not in inter_unicodes
            ]
            removed_unicodes += len(conflicts)

    merged = []
    seen: set[str] = set()
    previous_order = state.get("mergedGlyphOrder", [])
    if previous_order:
        for name in previous_order:
            glyph = inter_by_name.get(name, base_by_name.get(name))
            if glyph is not None and name not in seen:
                merged.append(glyph)
                seen.add(name)
    else:
        for glyph in base_glyphs:
            merged.append(inter_by_name.get(glyph.name, glyph))
            seen.add(glyph.name)
    for glyph in base_glyphs:
        if glyph.name not in seen:
            merged.append(glyph)
            seen.add(glyph.name)
    for glyph in inter_font.glyphs:
        if glyph.name not in seen:
            merged.append(inter_by_name[glyph.name])
            seen.add(glyph.name)
    font.glyphs.setter(merged)

    state["importedGlyphs"] = list(inter_by_name)
    state["mergedGlyphOrder"] = [glyph.name for glyph in merged]
    return len(overlap_names), removed_unicodes


def merge_into_astr(
    font,
    inter_font,
    state: dict,
    commit: str,
    settings: dict,
    replace_all: bool = False,
) -> tuple[int, int]:
    master_weights = tuple(settings["masterWeights"])
    export_weights = tuple(settings["exportWeights"])
    same_inter_commit = state.get("repositoryCommit") == commit
    baseline_changed = not math.isclose(
        _previous_astr_baseline(state),
        float(settings["astrBaseline"]),
        abs_tol=10**-GEOMETRY_DECIMAL_PLACES,
    )
    _ensure_axis_mappings(font, export_weights)
    _preserve_imported_component_positions(inter_font)
    text_records, display_records, changed_master_ids = _master_records(
        font, state, master_weights
    )
    target_by_coordinate = {
        (record["weight"], TEXT_OPSZ): record["id"] for record in text_records
    }
    target_by_coordinate.update(
        ((record["weight"], DISPLAY_OPSZ), record["id"])
        for record in display_records
    )
    master_map = {}
    for master in inter_font.masters:
        coordinate = (
            _normalize_weight(master.axes[0]),
            int(round(master.axes[1])),
        )
        master_map[master.id] = target_by_coordinate[coordinate]

    # A coordinate-only migration must not replace unchanged master layers.
    if (
        same_inter_commit
        and changed_master_ids
        and not replace_all
        and not baseline_changed
    ):
        print(
            "Replacing Inter data only in changed Astr masters: "
            + ", ".join(
                font.masters[master_id].name
                for master_id in changed_master_ids
            ),
            flush=True,
        )
        _replace_master_layers(
            font, inter_font, master_map, changed_master_ids
        )
        kerning_keys = set(state.get("kerningKeys", []))
        kerning_keys.update(_inter_kerning_keys(inter_font))
        _purge_kerning(font, kerning_keys, changed_master_ids)
        _merge_kerning(font, inter_font, master_map, changed_master_ids)
        _extend_inter_contextual_case(font)
        state["version"] = SYNC_STATE_VERSION
        state["repositoryCommit"] = commit
        state["settings"] = deepcopy(settings)
        state["kerningKeys"] = sorted(_inter_kerning_keys(inter_font))
        font.userData[SYNC_STATE_KEY] = state
        return 0, 0

    existing_markers = {
        glyph.name: glyph.userData.get(IMPORTED_GLYPH_KEY)
        for glyph in font.glyphs
        if glyph.userData.get(IMPORTED_GLYPH_KEY)
    }
    _restore_layout(font, state)
    _remove_previous_inter_glyphs(font, state)
    incoming_names = {glyph.name for glyph in inter_font.glyphs}
    _shift_astr_owned_glyphs(
        font,
        state,
        settings,
        incoming_names,
    )
    print("Mirroring Astr layers across the opsz endpoints...", flush=True)
    _mirror_astr_layers(font, text_records, display_records)
    _ensure_display_instances(font, text_records, display_records)

    old_overlap_glyphs = [
        glyph for glyph in font.glyphs if glyph.name in incoming_names
    ]
    purge_keys = set(state.get("kerningKeys", []))
    purge_keys.update(_inter_kerning_keys(inter_font))
    for glyph in old_overlap_glyphs:
        purge_keys.add(glyph.name)
        purge_keys.add(glyph.id)
        purge_keys.update(_kerning_group_keys(glyph))
    _purge_kerning(font, purge_keys)

    print("Replacing glyphs, kerning, and OpenType layout with Inter data...", flush=True)
    overlap, removed_unicodes = _merge_glyphs(
        font, inter_font, master_map, state, existing_markers
    )
    _merge_kerning(font, inter_font, master_map)
    _replace_layout(font, inter_font, state)
    _extend_inter_contextual_case(font)

    state["version"] = SYNC_STATE_VERSION
    state["repositoryCommit"] = commit
    state["settings"] = deepcopy(settings)
    state["kerningKeys"] = sorted(_inter_kerning_keys(inter_font))
    font.userData[SYNC_STATE_KEY] = state
    return overlap, removed_unicodes


def _serialize_object(obj, format_version: int) -> bytes:
    buffer = StringIO()
    Writer(buffer, format_version=format_version).write(obj)
    return buffer.getvalue().encode("utf-8")


def remove_empty_backgrounds(font) -> None:
    """Discard empty layers materialized internally by glyphsLib proxies."""
    empty = b"{\n}\n"
    for glyph in font.glyphs:
        for layer in glyph.layers:
            background = layer._background
            if (
                background is not None
                and _serialize_object(background, font.format_version) == empty
            ):
                layer._background = None


def _serialize_fontinfo(font) -> bytes:
    glyphs = font._glyphs
    font._glyphs = []
    try:
        buffer = StringIO()
        Writer(buffer, format_version=font.format_version).write(font)
        fontinfo = buffer.getvalue().replace("\nglyphs = (\n);\n", "\n", 1)
    finally:
        font._glyphs = glyphs
    return fontinfo.encode("utf-8")


def _serialize_order(font) -> bytes:
    buffer = BytesIO()
    openstep_plist.dump(
        [glyph.name for glyph in font.glyphs],
        buffer,
        unicode_escape=False,
        indent=0,
        single_line_tuples=True,
    )
    return buffer.getvalue()


def _write_if_changed(path: Path, data: bytes) -> bool:
    if path.is_file() and path.read_bytes() == data:
        return False
    temporary = path.with_name(
        f".{path.name}.sync-{os.getpid()}-{uuid.uuid4().hex}"
    )
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _replace_xml_attribute(tag: str, name: str, value: int | float) -> str:
    rendered_value = _format_number(value)
    pattern = re.compile(rf"(\b{re.escape(name)}\s*=\s*)([\"'])(.*?)(\2)")
    replaced, count = pattern.subn(
        lambda match: (
            f"{match.group(1)}{match.group(2)}"
            f"{rendered_value}{match.group(4)}"
        ),
        tag,
        count=1,
    )
    if count != 1:
        raise ValueError(f"Expected XML attribute {name!r} in {tag!r}")
    return replaced


def _xml_attribute(tag: str, name: str) -> str | None:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*([\"'])(.*?)\1", tag)
    return None if match is None else match.group(2)


def _replace_weight_dimension(block: str, weight: Weight) -> str:
    dimension_pattern = re.compile(r"<dimension\b[^>]*>")
    replaced = False

    def replace_dimension(match: re.Match[str]) -> str:
        nonlocal replaced
        tag = match.group(0)
        if replaced or _xml_attribute(tag, "name") != "Weight":
            return tag
        replaced = True
        return _replace_xml_attribute(tag, "xvalue", weight)

    result = dimension_pattern.sub(replace_dimension, block)
    if not replaced:
        raise ValueError("Designspace item has no Weight dimension")
    return result


def _replace_designspace_item_weights(
    xml: str,
    section_name: str,
    item_name: str,
    weights: tuple[Weight, ...],
) -> str:
    section_pattern = re.compile(
        rf"(<{section_name}\b[^>]*>)(.*?)(</{section_name}>)", re.DOTALL
    )
    section_match = section_pattern.search(xml)
    if section_match is None:
        raise ValueError(f"Astr.designspace has no {section_name} section")
    item_pattern = re.compile(
        rf"(<{item_name}\b[^>]*>.*?</{item_name}>)", re.DOTALL
    )
    index = 0

    def replace_item(match: re.Match[str]) -> str:
        nonlocal index
        if index >= len(weights):
            raise ValueError(
                f"Astr.designspace has more than {len(weights)} {item_name} items"
            )
        result = _replace_weight_dimension(match.group(0), weights[index])
        index += 1
        return result

    content = item_pattern.sub(replace_item, section_match.group(2))
    if index != len(weights):
        raise ValueError(
            f"Expected {len(weights)} {item_name} items in Astr.designspace, got {index}"
        )
    replacement = section_match.group(1) + content + section_match.group(3)
    return xml[: section_match.start()] + replacement + xml[section_match.end() :]


def render_astr_designspace(
    path: Path,
    master_weights: tuple[Weight, ...],
    export_weights: tuple[Weight, ...],
) -> bytes:
    xml = path.read_text(encoding="utf-8")
    root = ET.fromstring(xml)
    wght_axes = [
        axis
        for axis in root.findall("./axes/axis")
        if axis.get("tag") == "wght"
    ]
    if len(wght_axes) != 1:
        raise ValueError("Astr.designspace must contain exactly one wght axis")
    if len(root.findall("./sources/source")) != len(master_weights) * 2:
        raise ValueError("Astr.designspace must contain twelve sources")
    if len(root.findall("./instances/instance")) != len(export_weights) * 2:
        raise ValueError("Astr.designspace must contain ten instances")

    axis_pattern = re.compile(r"(<axis\b[^>]*>.*?</axis>)", re.DOTALL)
    axis_replaced = False

    def replace_axis(match: re.Match[str]) -> str:
        nonlocal axis_replaced
        block = match.group(0)
        opening_tag = block[: block.index(">") + 1]
        if _xml_attribute(opening_tag, "tag") != "wght":
            return block
        axis_replaced = True
        expected_mapping = _weight_axis_mapping(export_weights)
        seen: set[str] = set()

        def replace_map(map_match: re.Match[str]) -> str:
            tag = map_match.group(0)
            public_weight = _xml_attribute(tag, "input")
            if public_weight not in expected_mapping:
                raise ValueError(
                    f"Unexpected wght map input in Astr.designspace: {public_weight}"
                )
            seen.add(public_weight)
            return _replace_xml_attribute(
                tag, "output", expected_mapping[public_weight]
            )

        result = re.sub(r"<map\b[^>]*/>", replace_map, block)
        if seen != set(expected_mapping):
            raise ValueError(
                "Astr.designspace does not have the five expected wght maps"
            )
        return result

    xml = axis_pattern.sub(replace_axis, xml)
    if not axis_replaced:
        raise ValueError("Could not update the wght axis in Astr.designspace")
    xml = _replace_designspace_item_weights(
        xml,
        "sources",
        "source",
        master_weights + master_weights,
    )
    xml = _replace_designspace_item_weights(
        xml,
        "instances",
        "instance",
        export_weights + export_weights,
    )
    if not xml.endswith("\n"):
        xml += "\n"

    rendered = xml.encode("utf-8")
    document = DesignSpaceDocument.fromstring(rendered)
    actual_sources = tuple(
        _normalize_weight(source.designLocation["Weight"])
        for source in document.sources
    )
    actual_instances = tuple(
        _normalize_weight(instance.designLocation["Weight"])
        for instance in document.instances
    )
    if actual_sources != master_weights + master_weights:
        raise ValueError("Astr.designspace source weights were not updated correctly")
    if actual_instances != export_weights + export_weights:
        raise ValueError("Astr.designspace export weights were not updated correctly")
    return rendered


def write_glyphs_package(
    font, target: Path, removed_names: set[str]
) -> tuple[int, int, int]:
    glyph_directory = target / "glyphs"
    glyph_directory.mkdir(exist_ok=True)
    fontinfo_written = int(
        _write_if_changed(target / "fontinfo.plist", _serialize_fontinfo(font))
    )
    print(f"Comparing {len(font.glyphs)} generated glyph files...", flush=True)
    glyphs_written = 0
    for glyph in font.glyphs:
        path = glyph_directory / (userNameToFileName(glyph.name) + ".glyph")
        glyphs_written += int(
            _write_if_changed(
                path,
                _serialize_object(glyph, font.format_version),
            )
        )

    final_files = {
        userNameToFileName(glyph.name) + ".glyph" for glyph in font.glyphs
    }
    glyphs_removed = 0
    for name in sorted(removed_names):
        filename = userNameToFileName(name) + ".glyph"
        if filename in final_files:
            continue
        path = glyph_directory / filename
        if path.is_file():
            path.unlink()
            glyphs_removed += 1
    _write_if_changed(target / "order.plist", _serialize_order(font))
    return glyphs_written, glyphs_removed, fontinfo_written


def validate_merged_font(font, commit: str, settings: dict) -> None:
    master_weights = tuple(settings["masterWeights"])
    export_weights = tuple(settings["exportWeights"])
    text_master_specs = _master_specs(master_weights)
    text_instance_specs = _instance_specs(master_weights, export_weights)
    if font.familyName != FAMILY_NAME:
        raise ValueError(
            f"Expected family name {FAMILY_NAME!r}, got {font.familyName!r}"
        )
    for key, expected, _localized in FONT_METADATA:
        actual = font.properties.get(key)
        if actual != expected:
            raise ValueError(
                f"Unexpected Astr metadata {key}: expected {expected!r}, "
                f"got {actual!r}"
            )
    axes = [(axis.axisTag, axis.name) for axis in font.axes]
    if [tag for tag, _ in axes] != ["wght", "opsz"]:
        raise ValueError(f"Unexpected axes after sync: {axes}")
    calt = next((feature for feature in font.features if feature.name == "calt"), None)
    expected_contextual_code = (
        f"@{ASTR_CONTEXTUAL_UPPERCASE_CLASS}",
        f"@{ASTR_CONTEXTUAL_LOWERCASE_CLASS}",
        HANGUL_SYLLABLE_TOKEN,
        HANGUL_COMPATIBILITY_JAMO_TOKEN,
    )
    if calt is None or any(item not in calt.code for item in expected_contextual_code):
        raise ValueError(
            "Inter calt does not map Hangul syllables to contextual uppercase "
            "and compatibility jamo to contextual lowercase"
        )
    axis_mappings = font.customParameters["Axis Mappings"] or {}
    if axis_mappings.get("wght") != _weight_axis_mapping(export_weights):
        raise ValueError("The Astr wght axis mapping does not match its exports")
    if len(font.masters) != len(master_weights) * 2:
        raise ValueError(f"Expected 12 masters after sync, got {len(font.masters)}")
    expected_masters = [
        (name, [weight, TEXT_OPSZ])
        for _master_id, name, weight in text_master_specs
    ] + [
        (f"Display {name}", [weight, DISPLAY_OPSZ])
        for _master_id, name, weight in text_master_specs
    ]
    actual_masters = [(master.name, list(master.axes)) for master in font.masters]
    if actual_masters != expected_masters:
        raise ValueError(
            "Unexpected Astr master names or coordinates: "
            f"{actual_masters}"
        )
    if font.customParameters["Variable Font Origin"] != REGULAR_MASTER_ID:
        raise ValueError("The Regular master must be the variable-font origin")
    lower_percent_circle = font.glyphs["_zero_percent1"]
    if lower_percent_circle is None:
        raise ValueError("Imported Inter helper _zero_percent1 is missing")
    for master in font.masters:
        components = lower_percent_circle.layers[master.id].components
        if len(components) != 1 or components[0].position.y >= 0:
            raise ValueError(
                "_zero_percent1 must retain its negative component offset in "
                f"master {master.name}"
            )
    state = font.userData.get(SYNC_STATE_KEY)
    if (
        not state
        or state.get("repositoryCommit") != commit
        or int(state.get("version", 0)) != SYNC_STATE_VERSION
        or state.get("settings") != settings
    ):
        raise ValueError("Inter sync metadata was not saved correctly")
    if settings.get("astrBaseline") and state.get("importedGlyphs"):
        imported_names = set(state["importedGlyphs"])
        for glyph in font.glyphs:
            if glyph.userData.get(IMPORTED_GLYPH_KEY):
                continue
            for layer in glyph.layers:
                for component in layer.components:
                    if (
                        component.name in imported_names
                        and component.alignment != -1
                    ):
                        raise ValueError(
                            "Astr component referencing an Inter glyph must keep "
                            "its baseline offset: "
                            f"{glyph.name}, {layer.name}, {component.name}"
                        )
    display_instances = [
        instance
        for instance in font.instances
        if len(instance.axes) >= 2
        and int(round(instance.axes[1])) == DISPLAY_OPSZ
        and instance.properties.get("familyNames") == DISPLAY_FAMILY
    ]
    if len(display_instances) != 5:
        raise ValueError(
            f"Expected five {DISPLAY_FAMILY} instances, got {len(display_instances)}"
        )
    text_instances = [
        instance
        for instance in font.instances
        if len(instance.axes) >= 2
        and int(round(instance.axes[1])) == TEXT_OPSZ
        and instance.active
    ]
    expected_instances = [
        (name, [weight, TEXT_OPSZ], OrderedDict(interpolations))
        for name, weight, _weight_class, interpolations in text_instance_specs
    ]
    actual_instances = [
        (
            instance.name,
            list(instance.axes),
            OrderedDict(instance.instanceInterpolations),
        )
        for instance in text_instances
    ]
    if actual_instances != expected_instances:
        raise ValueError(
            "Unexpected Astr Text exports after sync: "
            f"{actual_instances}"
        )
    expected_layers = {master.id for master in font.masters}
    for glyph in font.glyphs:
        if glyph.userData.get(IMPORTED_GLYPH_KEY):
            master_layers = {
                layer.layerId for layer in glyph.layers if layer._is_master_layer
            }
            if master_layers != expected_layers:
                raise ValueError(f"Imported glyph {glyph.name} is missing master layers")
            for layer in glyph.layers:
                if not layer._is_master_layer:
                    continue
                for component in layer.components:
                    if component.alignment != -1:
                        raise ValueError(
                            "Imported Inter component must have automatic alignment "
                            f"disabled: {glyph.name}, {layer.name}, {component.name}"
                        )


def read_sync_state(path: Path) -> dict:
    fontinfo = openstep_plist.loads(
        (path / "fontinfo.plist").read_text(encoding="utf-8"),
        use_numbers=True,
    )
    return dict(fontinfo.get("userData", {}).get(SYNC_STATE_KEY) or {})


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch current Inter and merge it into the Astr Glyphs package"
    )
    parser.add_argument(
        "--source",
        default=str(DEFAULT_FONT_SOURCE),
        help=(
            "Glyphs package to update "
            "(default: sources/Astr.glyphspackage)"
        ),
    )
    parser.add_argument(
        "--repository",
        default=DEFAULT_REPOSITORY,
        help="Inter git repository URL (default: official rsms/inter repository)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="reapply every Inter glyph even when the commit is already synchronized",
    )
    parser.add_argument(
        "--initialize",
        action="store_true",
        help=(
            "first convert the original Asta Sans masters, exports, family name, "
            "and axes to the Astr project structure"
        ),
    )
    parser.add_argument(
        "--scale",
        type=parse_scale,
        help=(
            "Inter outline scale used by --initialize; accepts a ratio or "
            "percentage such as 0.96, 96, or 96%%"
        ),
    )
    parser.add_argument(
        "--astr-baseline",
        type=float,
        help=(
            "Astr-owned glyph baseline offset used by --initialize, expressed "
            "in 1000-UPM units; positive values move Astr glyphs upward"
        ),
    )
    parser.add_argument(
        "--master-weights",
        type=parse_weights,
        help=(
            "six comma- or space-separated Inter/Astr master coordinates; "
            "decimals are accepted"
        ),
    )
    parser.add_argument(
        "--export-weights",
        type=parse_weights,
        help=(
            "five comma- or space-separated Astr export coordinates; "
            "decimals are accepted"
        ),
    )
    args = parser.parse_args()

    font_source = Path(args.source).expanduser()
    if not font_source.is_absolute():
        font_source = ROOT / font_source
    font_source = font_source.resolve()
    if not font_source.is_dir():
        raise FileNotFoundError(f"Glyphs package not found: {font_source}")

    saved_state = read_sync_state(font_source)
    supplied_settings = any(
        value is not None
        for value in (
            args.scale,
            args.astr_baseline,
            args.master_weights,
            args.export_weights,
        )
    )
    if supplied_settings and not args.initialize:
        parser.error(
            "--scale, --astr-baseline, --master-weights, and --export-weights "
            "can only be used with --initialize"
        )
    if args.initialize:
        try:
            settings = _settings_dict(
                DEFAULT_SCALE if args.scale is None else args.scale,
                DEFAULT_ASTR_BASELINE
                if args.astr_baseline is None
                else args.astr_baseline,
                DEFAULT_MASTER_WEIGHTS
                if args.master_weights is None
                else args.master_weights,
                DEFAULT_EXPORT_WEIGHTS
                if args.export_weights is None
                else args.export_weights,
            )
        except ValueError as error:
            parser.error(str(error))
        designspace_data = render_astr_designspace(
            ASTR_DESIGNSPACE,
            tuple(settings["masterWeights"]),
            tuple(settings["exportWeights"]),
        )
    else:
        settings = _settings_from_state(saved_state)
        designspace_data = None

    inter_source, commit = fetch_latest_inter(args.repository)
    print(f"Using Inter commit {commit}", flush=True)
    if (
        not args.force
        and not args.initialize
        and int(saved_state.get("version", 0)) >= SYNC_STATE_VERSION
        and saved_state.get("repositoryCommit") == commit
    ):
        print("Astr already contains this Inter commit; nothing to update.")
        return

    astr = glyphsLib.GSFont(str(font_source))
    if args.initialize:
        print(
            "Configuring the Asta Sans package as the Astr source project...",
            flush=True,
        )
        configure_astr_project(astr, settings)
    before_names = {glyph.name for glyph in astr.glyphs}
    state = deepcopy(astr.userData.get(SYNC_STATE_KEY) or {})
    with tempfile.TemporaryDirectory(prefix="astr-inter-sync-") as directory:
        inter = prepare_inter_font(
            inter_source,
            Path(directory),
            float(astr.upm),
            settings,
        )
        overlap, removed_unicodes = merge_into_astr(
            astr,
            inter,
            state,
            commit,
            settings,
            replace_all=args.force or args.initialize,
        )
    remove_empty_backgrounds(astr)
    validate_merged_font(astr, commit, settings)
    final_names = {glyph.name for glyph in astr.glyphs}
    glyphs_written, glyphs_removed, fontinfo_written = write_glyphs_package(
        astr, font_source, before_names - final_names
    )
    designspace_written = False
    if designspace_data is not None:
        designspace_written = _write_if_changed(ASTR_DESIGNSPACE, designspace_data)
    print(
        f"Synced {len(inter.glyphs)} Inter glyphs at 12 masters; "
        f"replaced {overlap} Astr glyph names and removed "
        f"{removed_unicodes} conflicting Unicode assignments. "
        f"Wrote {glyphs_written} changed glyph files, removed "
        f"{glyphs_removed}, and updated fontinfo={bool(fontinfo_written)}, "
        f"designspace={designspace_written}."
    )


if __name__ == "__main__":
    main()
