from pathlib import Path
from fontTools.ttLib import TTFont

font_paths = [
    *Path("fonts").glob("**/*.ttf"),
    *Path("fonts").glob("**/*.woff2"),
]

for file in font_paths:
    font = TTFont(str(file))
    if font["gasp"].gaspRange != {65535: 0x000A}:
        font["gasp"].gaspRange = {65535: 0x000A}

    try:
        del font["prep"]
    except KeyError:
        pass

    font.save(file)
