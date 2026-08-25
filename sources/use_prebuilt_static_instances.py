from __future__ import annotations

import argparse
from pathlib import Path
import re


EXPECTED_INSTANCE_COUNT = 10
INSTANCE_PREFIX = "masters/instance_ufos/"


def generated_blocks(text: str) -> list[str]:
    """Split a gftools-builder Ninja file without changing block contents."""
    starts = [match.start() for match in re.finditer(r"(?m)^# Generating ", text)]
    if not starts:
        return [text]
    boundaries = [0, *starts, len(text)]
    return [
        text[boundaries[index] : boundaries[index + 1]]
        for index in range(len(boundaries) - 1)
        if boundaries[index] != boundaries[index + 1]
    ]


def instance_target(block: str) -> str | None:
    if not re.search(r"(?m)^  operation = instantiateUfo$", block):
        return None
    # Ninja may wrap the rule name onto the following line when the output
    # path is long, so identify the edge by output and operation separately.
    match = re.search(
        rf"(?m)^build ({re.escape(INSTANCE_PREFIX)}[^:]+):", block
    )
    return match.group(1) if match else None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Use the static UFOs prepared in one fontmake process instead of "
            "repeating designspace interpolation in every Ninja job."
        )
    )
    parser.add_argument("ninja_file", type=Path)
    args = parser.parse_args()

    ninja_file = args.ninja_file.resolve()
    text = ninja_file.read_text()
    kept: list[str] = []
    removed: list[str] = []
    for block in generated_blocks(text):
        target = instance_target(block)
        if target is None:
            kept.append(block)
        else:
            removed.append(target)

    if len(removed) != EXPECTED_INSTANCE_COUNT:
        raise ValueError(
            "Expected to replace exactly "
            f"{EXPECTED_INSTANCE_COUNT} static interpolation jobs, got "
            f"{len(removed)}. The gftools-builder graph may have changed."
        )

    missing = [
        target for target in removed if not (ninja_file.parent / target).is_file()
    ]
    if missing:
        raise FileNotFoundError(
            "Prebuilt static UFOs are missing; run `make converter` first: "
            + ", ".join(missing)
        )

    ninja_file.write_text("".join(kept))
    print(
        f"Using {len(removed)} prebuilt static UFOs; "
        "removed redundant interpolation jobs"
    )


if __name__ == "__main__":
    main()
