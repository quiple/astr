from __future__ import annotations

import math
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

from fontTools.designspaceLib import (
    DesignSpaceDocument,
    InstanceDescriptor,
    SourceDescriptor,
)


SOURCES = Path(__file__).resolve().parent
SOURCE_DESIGNSPACE = SOURCES / "Aster.designspace"
MASTER_DIRECTORY = SOURCES / "masters"
INSTANCE_DESIGNSPACE = MASTER_DIRECTORY / "Aster-default-instances.designspace"
BUILD_DESIGNSPACE = MASTER_DIRECTORY / "Aster.designspace"
STATIC_INSTANCE_DIRECTORY = MASTER_DIRECTORY / "instance_ufos"
DEFAULT_FAMILY_NAME = "Aster"
DEFAULT_STYLE_NAME = "Regular"


def tool(name: str) -> str:
    path = Path(sys.executable).with_name(name)
    if not path.is_file():
        raise FileNotFoundError(
            f"{name} was not found next to {sys.executable}; run `make setup` first"
        )
    return str(path)


def axis(document: DesignSpaceDocument, tag: str):
    matches = [item for item in document.axes if item.tag == tag]
    if len(matches) != 1:
        raise ValueError(f"Expected one {tag} axis, got {len(matches)}")
    return matches[0]


def same_location(left: dict[str, float], right: dict[str, float]) -> bool:
    return left.keys() == right.keys() and all(
        math.isclose(left[key], right[key], abs_tol=1e-6) for key in left
    )


def source_at(
    document: DesignSpaceDocument, location: dict[str, float]
) -> SourceDescriptor | None:
    for source in document.sources:
        if same_location(source.getFullDesignLocation(document), location):
            return source
    return None


def default_locations(
    document: DesignSpaceDocument,
) -> tuple[dict[str, float], dict[str, float]]:
    weight_axis = axis(document, "wght")
    optical_axis = axis(document, "opsz")
    default_weight = weight_axis.map_forward(weight_axis.default)
    text_optical_size = optical_axis.map_forward(optical_axis.minimum)
    display_optical_size = optical_axis.map_forward(optical_axis.maximum)
    return (
        {
            weight_axis.name: default_weight,
            optical_axis.name: text_optical_size,
        },
        {
            weight_axis.name: default_weight,
            optical_axis.name: display_optical_size,
        },
    )


def materialize_missing_defaults(
    document: DesignSpaceDocument,
    targets: tuple[tuple[str, Path, dict[str, float]], ...],
) -> None:
    missing = [
        target for target in targets if source_at(document, target[2]) is None
    ]
    if not missing:
        return

    instance_document = DesignSpaceDocument.fromfile(SOURCE_DESIGNSPACE)
    base_sources = [
        source
        for source in instance_document.sources
        if source.copyInfo or source.copyLib or source.copyFeatures
    ]
    if len(base_sources) != 1:
        raise ValueError(
            "Expected exactly one metadata source for default interpolation, "
            f"got {len(base_sources)}"
        )
    base_location = base_sources[0].getFullDesignLocation(instance_document)
    source_locations = [
        source.getFullDesignLocation(instance_document)
        for source in instance_document.sources
    ]
    # fontmake itself requires an existing default source in order to
    # interpolate instances. This temporary designspace therefore works only
    # in design coordinates, with the editable Regular master as its base. The
    # final build designspace restores the public axis mapping and uses the
    # newly interpolated wght=400 source as its base.
    for item in instance_document.axes:
        values = [location[item.name] for location in source_locations]
        item.map = []
        item.minimum = min(values)
        item.default = base_location[item.name]
        item.maximum = max(values)
    instance_document.instances = []
    for name, path, location in missing:
        if path.exists():
            shutil.rmtree(path)
        instance = InstanceDescriptor()
        instance.name = name
        # `name` must distinguish the two build-only instances, but their
        # family/style metadata becomes the variable font's default names.
        instance.familyName = DEFAULT_FAMILY_NAME
        instance.styleName = DEFAULT_STYLE_NAME
        instance.path = str(path)
        instance.designLocation = location
        instance_document.addInstance(instance)
    instance_document.write(INSTANCE_DESIGNSPACE)

    subprocess.run(
        [
            tool("fontmake"),
            "-m",
            str(INSTANCE_DESIGNSPACE),
            "-o",
            "ufo",
            "-i",
            "Aster Regular Build.*",
            "--expand-features-to-instances",
        ],
        check=True,
    )
    absent = [
        str(path) for _name, path, _location in missing if not path.is_dir()
    ]
    if absent:
        raise FileNotFoundError(
            "fontmake did not create the interpolated defaults: " + ", ".join(absent)
        )
    for _name, path, _location in missing:
        info_path = path / "fontinfo.plist"
        with info_path.open("rb") as file:
            info = plistlib.load(file)
        info["familyName"] = DEFAULT_FAMILY_NAME
        info["styleName"] = DEFAULT_STYLE_NAME
        # The generated defaults are sources, not final static instances. If
        # their instance-only class 400 remains while the editable sources omit
        # this field, fontmake tries to interpolate OS/2 weight classes and can
        # produce a non-integer value. Final static instances receive their
        # explicit 200/300/400/500/600 classes from Aster.designspace instead.
        info.pop("openTypeOS2WeightClass", None)
        with info_path.open("wb") as file:
            plistlib.dump(info, file, sort_keys=False)


def build_designspace(
    targets: tuple[tuple[str, Path, dict[str, float]], ...]
) -> None:
    document = DesignSpaceDocument.fromfile(SOURCE_DESIGNSPACE)
    for source in document.sources:
        source.copyLib = False
        source.copyGroups = False
        source.copyFeatures = False
        source.copyInfo = False

    for name, path, location in targets:
        source = source_at(document, location)
        if source is None:
            source = SourceDescriptor()
            source.name = name
            source.familyName = DEFAULT_FAMILY_NAME
            source.styleName = DEFAULT_STYLE_NAME
            source.path = str(path)
            source.designLocation = location
            document.addSource(source)

    text_location = targets[0][2]
    base_source = source_at(document, text_location)
    if base_source is None:
        raise ValueError("Could not create the variable font's default source")
    base_source.copyLib = True
    base_source.copyGroups = True
    base_source.copyFeatures = True
    base_source.copyInfo = True

    locations = [
        source.getFullDesignLocation(document) for source in document.sources
    ]
    for index, location in enumerate(locations):
        if any(same_location(location, other) for other in locations[:index]):
            raise ValueError(f"Duplicate build source location: {location}")
    document.write(BUILD_DESIGNSPACE)


def materialize_static_instances() -> None:
    document = DesignSpaceDocument.fromfile(BUILD_DESIGNSPACE)
    expected = {
        Path(instance.filename).name + ".json" for instance in document.instances
    }
    print(
        f"Materializing {len(expected)} static UFOs in one fontmake process...",
        flush=True,
    )
    if STATIC_INSTANCE_DIRECTORY.exists():
        shutil.rmtree(STATIC_INSTANCE_DIRECTORY)
    subprocess.run(
        [
            tool("fontmake"),
            "-m",
            str(BUILD_DESIGNSPACE),
            "-o",
            "ufo",
            "-i",
            "--ufo-structure=json",
            "--output-dir",
            str(STATIC_INSTANCE_DIRECTORY),
        ],
        check=True,
    )

    actual = {path.name for path in STATIC_INSTANCE_DIRECTORY.glob("*.ufo.json")}
    if actual != expected:
        raise ValueError(
            "Static instance generation did not create the expected UFOs: "
            f"expected {sorted(expected)}, got {sorted(actual)}"
        )


def main() -> None:
    MASTER_DIRECTORY.mkdir(parents=True, exist_ok=True)
    document = DesignSpaceDocument.fromfile(SOURCE_DESIGNSPACE)
    text_location, display_location = default_locations(document)
    targets = (
        (
            "Aster Regular Build Text",
            MASTER_DIRECTORY / "Aster-VFDefault.ufo",
            text_location,
        ),
        (
            "Aster Regular Build Display",
            MASTER_DIRECTORY / "Aster-DisplayVFDefault.ufo",
            display_location,
        ),
    )
    materialize_missing_defaults(document, targets)
    build_designspace(targets)
    # gftools-builder normally starts one fontmake process per instance. With
    # Aster's large glyph set, those processes each load all 14 sources and can
    # exhaust memory. Generate all ten instances in one process instead; the
    # static build then removes those redundant jobs from its Ninja graph.
    materialize_static_instances()
    print(f"Prepared variable-font sources at {BUILD_DESIGNSPACE}")


if __name__ == "__main__":
    main()
