from pathlib import Path

from fontTools.ttLib import TTFont


ROOT = Path(__file__).resolve().parent.parent
FONT_ROOT = ROOT / "fonts"
OUTPUT_DIRECTORY = FONT_ROOT / "webfonts"
TTF_DIRECTORIES = (FONT_ROOT / "variable", FONT_ROOT / "ttf")


def ttf_sources() -> list[Path]:
    return sorted(
        source
        for directory in TTF_DIRECTORIES
        for source in directory.glob("*.ttf")
    )


def compress(source: Path, destination: Path) -> bool:
    if (
        destination.is_file()
        and destination.stat().st_mtime_ns >= source.stat().st_mtime_ns
    ):
        print(f"Up to date: {destination.relative_to(ROOT)}")
        return False

    temporary = destination.with_name(f".{destination.name}.tmp")
    font = TTFont(source, recalcTimestamp=False)
    try:
        font.flavor = "woff2"
        font.save(temporary)
        temporary.replace(destination)
    finally:
        font.close()
        temporary.unlink(missing_ok=True)
    print(f"Wrote: {destination.relative_to(ROOT)}")
    return True


def main() -> None:
    sources = ttf_sources()
    if not sources:
        raise SystemExit(
            "No TTF files found in fonts/variable or fonts/ttf; "
            "build the TTFs first."
        )

    destinations: dict[str, Path] = {}
    for source in sources:
        previous = destinations.get(source.stem)
        if previous is not None:
            raise SystemExit(
                f"Duplicate TTF filename {source.name}: {previous} and {source}"
            )
        destinations[source.stem] = source

    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    written = sum(
        compress(source, OUTPUT_DIRECTORY / f"{source.stem}.woff2")
        for source in sources
    )
    print(f"WOFF2: {written} written, {len(sources) - written} up to date")


if __name__ == "__main__":
    main()
