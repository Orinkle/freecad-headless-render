"""
Material assignment.

FreeCAD opened headlessly (``import FreeCAD``) has **no ViewObject**, so the
per-part colors stored in the .FCStd file are unreachable. Materials must
therefore come from one of two places:

1. an explicit mapping file (``--materials mats.json``), or
2. a palette, allocated by name pattern first and by round-robin after that.

The name patterns exist for a boring reason: a gearbox exports as
``SunGear``/``PlanetGear_1``/``PlanetGear_2``/``RingGear``, and round-robin
would paint the three identical planets three different colors. Pattern
matching keeps siblings together.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Sequence


RGB = tuple[float, float, float]


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


@dataclass(frozen=True)
class Material:
    """A flat lit surface.

    ``color`` is **sRGB** in 0..1 -- that is what a colour picker hands you and
    what every palette below is written in. Shading happens in linear space, so
    use :attr:`linear` when feeding the rasteriser.
    """

    color: RGB
    specular: float = 0.60
    shininess: float = 50.0

    @property
    def linear(self) -> RGB:
        c = self.color
        return (srgb_to_linear(c[0]), srgb_to_linear(c[1]), srgb_to_linear(c[2]))

    def as_tuple(self) -> tuple:
        return (self.color, self.specular, self.shininess)


# Roles are resolved per-palette so a single rule table works everywhere.
# Order matters: first match wins, so `ring` must come before `gear`.
_NAME_RULES: Sequence[tuple[str, str]] = (
    (r"(sun|pinion|drive|input|motor)", "accent"),
    (r"(ring|annulus|internal)", "body"),
    (r"(planet|gear|sprocket|wheel|idler)", "steel"),
    (r"(shaft|axle|rod|bolt|screw|nut|bearing|pins?)", "chrome"),
    (r"(cover|lid|housing|frame|plate|base|bracket|shell)", "neutral"),
    (r"(carrier|arm|hub|flange|coupling)", "warm"),
)


PALETTES: dict[str, dict[str, Material]] = {
    # Cold machined metal, one warm accent. Good default for mechanisms.
    "metal": {
        "accent": Material((0.78, 0.48, 0.18), 0.55, 42),
        "steel": Material((0.63, 0.66, 0.70), 0.70, 60),
        "chrome": Material((0.80, 0.82, 0.84), 0.85, 80),
        "body": Material((0.33, 0.38, 0.45), 0.62, 55),
        "warm": Material((0.55, 0.40, 0.22), 0.50, 40),
        "neutral": Material((0.70, 0.70, 0.72), 0.55, 45),
    },
    # Brass-and-walnut; reads well on warm paper backgrounds.
    "warm": {
        "accent": Material((0.82, 0.55, 0.24), 0.62, 46),
        "steel": Material((0.72, 0.62, 0.48), 0.55, 42),
        "chrome": Material((0.86, 0.80, 0.70), 0.72, 70),
        "body": Material((0.48, 0.34, 0.22), 0.45, 36),
        "warm": Material((0.64, 0.44, 0.26), 0.50, 40),
        "neutral": Material((0.74, 0.68, 0.60), 0.50, 44),
    },
    # Draftsman's blueprint: cool blue-grey, low saturation.
    "blueprint": {
        "accent": Material((0.60, 0.72, 0.86), 0.55, 48),
        "steel": Material((0.48, 0.58, 0.72), 0.60, 52),
        "chrome": Material((0.72, 0.80, 0.90), 0.70, 70),
        "body": Material((0.30, 0.40, 0.55), 0.50, 45),
        "warm": Material((0.44, 0.54, 0.68), 0.50, 44),
        "neutral": Material((0.58, 0.66, 0.78), 0.50, 46),
    },
    # Single clay color; use with --light for silhouette / form studies.
    "mono": {
        "accent": Material((0.72, 0.72, 0.72), 0.55, 48),
        "steel": Material((0.68, 0.68, 0.68), 0.55, 48),
        "chrome": Material((0.78, 0.78, 0.78), 0.65, 60),
        "body": Material((0.55, 0.55, 0.55), 0.45, 42),
        "warm": Material((0.64, 0.64, 0.64), 0.50, 45),
        "neutral": Material((0.70, 0.70, 0.70), 0.52, 46),
    },
}

_ROLE_ORDER = ("accent", "steel", "chrome", "body", "warm", "neutral")


def _role_for(name: str) -> str | None:
    low = name.lower()
    for pattern, role in _NAME_RULES:
        if re.search(pattern, low):
            return role
    return None


def load_overrides(path: str) -> dict[str, Material]:
    """Read a JSON mapping of ``{"PartName": {...}}``.

    Each entry accepts ``{"color": [r,g,b], "specular": 0.6, "shininess": 50}``.
    Colors are linear 0..1; a ``#rrggbb`` string is also accepted.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    # Tolerate a top-level {"materials": {...}} wrapper.
    if isinstance(raw, dict) and "materials" in raw and isinstance(raw["materials"], dict):
        raw = raw["materials"]

    out: dict[str, Material] = {}
    for name, spec in raw.items():
        if isinstance(spec, (list, tuple)):
            spec = {"color": list(spec)}
        color = spec.get("color", [0.7, 0.7, 0.7])
        if isinstance(color, str):
            color = _hex_to_rgb(color)
        out[name] = Material(
            tuple(float(c) for c in color),  # type: ignore[arg-type]
            float(spec.get("specular", 0.6)),
            float(spec.get("shininess", 50.0)),
        )
    return out


def _hex_to_rgb(value: str) -> RGB:
    h = value.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"expected #rrggbb, got {value!r}")
    return tuple(int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def assign(
    names: Iterable[str],
    palette: str = "metal",
    overrides: dict[str, Material] | None = None,
) -> dict[str, Material]:
    """Map part name -> Material.

    Precedence: explicit override > name-pattern role > round-robin.

    Round-robin only consumes roles that the patterns did not claim, so an
    unnamed part never steals the gear's color.
    """
    if palette not in PALETTES:
        raise ValueError(f"unknown palette {palette!r}; choose from {sorted(PALETTES)}")
    table = PALETTES[palette]
    overrides = overrides or {}

    names = list(names)
    result: dict[str, Material] = {}
    claimed: set[str] = set()

    for name in names:
        if name in overrides:
            result[name] = overrides[name]
            continue
        role = _role_for(name)
        if role is not None:
            result[name] = table[role]
            claimed.add(role)

    free = [r for r in _ROLE_ORDER if r not in claimed] or list(_ROLE_ORDER)
    cursor = 0
    for name in names:
        if name in result:
            continue
        result[name] = table[free[cursor % len(free)]]
        cursor += 1

    return result
