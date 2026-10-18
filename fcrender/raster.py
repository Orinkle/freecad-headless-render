"""Numpy z-buffer rasteriser.

No OpenGL, no X11, no display server. One triangle at a time in plain Python,
which sounds slow and is -- about 4 s for a 56k-triangle assembly at 1080p
with 2x supersampling. For stills that is the right trade: zero dependencies
and no driver to fight.

Shading is flat (per-face normal), three directional lights, Blinn-Phong
specular, and a Fresnel rim term that keeps machined edges legible. It is not
a physically based renderer and does not try to be.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


Array = np.ndarray


@dataclass
class Light:
    direction: tuple[float, float, float]
    intensity: float = 1.0
    color: tuple[float, float, float] = (1.0, 1.0, 1.0)

    def unit(self) -> Array:
        d = np.asarray(self.direction, dtype=np.float64)
        return d / np.linalg.norm(d)


@dataclass
class Lighting:
    """Three-point rig. Directions point *from the surface toward the light*."""

    key: Light = field(default_factory=lambda: Light((-0.42, 0.62, 0.66), 0.70))
    fill: Light = field(default_factory=lambda: Light((0.75, 0.25, 0.60), 0.20))
    rim: Light = field(default_factory=lambda: Light((0.10, -0.85, 0.52), 0.14))
    ambient: float = 0.10
    exposure: float = 1.05
    rim_glow: float = 0.11
    specular_gain: float = 0.45


def srgb_lut(n: int = 4096) -> Array:
    """Linear 0..1 -> sRGB 0..255 lookup table."""
    x = np.linspace(0.0, 1.0, n)
    y = np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055)
    return np.clip(np.round(255 * y), 0, 255)


_LUT = srgb_lut()


def to_srgb8(linear: Array) -> Array:
    idx = (np.clip(linear, 0.0, 1.0) * (len(_LUT) - 1)).astype(np.int32)
    return _LUT[idx]


def shade(
    normals: Array,
    base: Array,
    specular: Array,
    shininess: Array,
    view_dir: Array,
    lighting: Lighting,
) -> Array:
    """Per-face RGB in linear space. All inputs are (m, ...)."""
    n = normals
    amb = lighting.ambient

    def lam(light: Light) -> Array:
        return np.clip(n @ light.unit(), 0.0, 1.0) * light.intensity

    lit = amb + lam(lighting.key) + lam(lighting.fill) + lam(lighting.rim)

    # Blinn-Phong with a single half vector (the key light dominates the look).
    v = -np.asarray(view_dir, dtype=np.float64)
    h = lighting.key.unit() + v
    hn = np.linalg.norm(h)
    h = h / hn if hn > 1e-9 else v
    spec = np.clip(n @ h, 0.0, 1.0) ** shininess * specular

    # Fresnel: brighten faces seen edge-on. Fakes the hard highlight that
    # makes turned metal read as metal.
    fres = (1.0 - np.clip(np.abs(n @ v), 0.0, 1.0)) ** 3

    # Specular is tinted by the base colour: a metal's highlight carries its
    # own hue. Adding white on top instead is what turns bronze into pink.
    col = (
        base * (lit * lighting.exposure + spec * lighting.specular_gain)[:, None]
        + fres[:, None] * lighting.rim_glow
    )
    return np.clip(col, 0.0, 1.0)


def rasterize(
    proj,
    colors: Array,
    width: int,
    height: int,
    ss: int = 2,
    background: tuple[float, float, float] | None = (0.965, 0.957, 0.941),
    visible: Array | None = None,
) -> Array:
    """Draw projected triangles into an RGBA uint8 image.

    `colors` must have one linear RGB row per triangle in ``proj``.
    `background=None` leaves untouched pixels fully transparent.
    """
    Wp, Hp = width * ss, height * ss

    sx = proj.sx * ss
    sy = proj.sy * ss
    depth = proj.depth

    buf = np.zeros((Hp, Wp, 3), dtype=np.float64)
    alpha = np.zeros((Hp, Wp), dtype=np.float64)
    if background is not None:
        buf[:] = np.asarray(background, dtype=np.float64)
        alpha[:] = 1.0

    zbuf = np.full((Hp, Wp), np.inf, dtype=np.float64)

    idx = np.arange(len(sx)) if visible is None else np.nonzero(visible)[0]
    if idx.size == 0:
        return _finalise(buf, alpha, width, height, ss, background)

    # Painter's order as a cheap early-out: front faces land first and later
    # fragments usually fail the depth test instead of overwriting.
    order = idx[np.argsort(depth[idx].mean(axis=1))]

    for i in order:
        x0, x1, x2 = sx[i]
        y0, y1, y2 = sy[i]

        minx = int(min(x0, x1, x2))
        maxx = int(max(x0, x1, x2)) + 1
        miny = int(min(y0, y1, y2))
        maxy = int(max(y0, y1, y2)) + 1
        if maxx <= 0 or maxy <= 0 or minx >= Wp or miny >= Hp:
            continue
        minx = max(minx, 0)
        miny = max(miny, 0)
        maxx = min(maxx, Wp)
        maxy = min(maxy, Hp)

        denom = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(denom) < 1e-12:
            continue

        xs = np.arange(minx, maxx, dtype=np.float64) + 0.5
        ys = np.arange(miny, maxy, dtype=np.float64) + 0.5
        px, py = np.meshgrid(xs, ys)

        w0 = ((y1 - y2) * (px - x2) + (x2 - x1) * (py - y2)) / denom
        w1 = ((y2 - y0) * (px - x2) + (x0 - x2) * (py - y2)) / denom
        w2 = 1.0 - w0 - w1
        inside = (w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9)
        if not inside.any():
            continue

        zz = w0 * depth[i, 0] + w1 * depth[i, 1] + w2 * depth[i, 2]

        z_win = zbuf[miny:maxy, minx:maxx]
        mask = inside & (zz < z_win)
        if not mask.any():
            continue
        z_win[mask] = zz[mask]
        buf[miny:maxy, minx:maxx][mask] = colors[i]
        alpha[miny:maxy, minx:maxx][mask] = 1.0

    return _finalise(buf, alpha, width, height, ss, background)


def _finalise(buf, alpha, width: int, height: int, ss: int, background) -> Array:
    if ss > 1:
        buf = buf.reshape(height, ss, width, ss, 3).mean(axis=(1, 3))
        alpha = alpha.reshape(height, ss, width, ss).mean(axis=(1, 3))
    rgb = to_srgb8(buf).astype(np.uint8)
    a = np.clip(np.round(alpha * 255), 0, 255).astype(np.uint8)
    return np.dstack([rgb, a[..., None]])
