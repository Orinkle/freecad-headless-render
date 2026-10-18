"""
Camera: orthographic or perspective, auto-fitted to the scene.

Sign convention, stated once because getting it wrong silently renders the
inside of your model: ``view_dir`` points **from the camera toward the
scene**, exactly like a light's direction vector. The camera therefore sits at
``center - view_dir * distance``. Screen depth is normalised so that
``0 == nearest``.

Users normally should not build this by hand -- pass ``--azimuth`` /
``--elevation`` or pick a named view.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


Array = np.ndarray


@dataclass
class Camera:
    view_dir: tuple[float, float, float] = (-0.62, 0.72, -0.50)
    up: tuple[float, float, float] = (0.0, 0.0, 1.0)
    ortho: bool = True
    fov: float = 32.0  # vertical field of view, degrees; perspective only
    pad: float = 1.06  # >1 leaves a margin around the silhouette

    @classmethod
    def from_angles(
        cls,
        azimuth: float,
        elevation: float,
        ortho: bool = True,
        fov: float = 32.0,
        pad: float = 1.06,
    ) -> "Camera":
        """`azimuth`/`elevation` in degrees describe where the camera *is*.

        azimuth 0 = +X axis, 90 = +Y axis; elevation 90 = straight above.
        """
        a = math.radians(azimuth)
        e = math.radians(elevation)
        # Camera position direction, then negate to get the view direction.
        cx = math.cos(e) * math.cos(a)
        cy = math.cos(e) * math.sin(a)
        cz = math.sin(e)
        up = (0.0, 0.0, 1.0)
        if abs(cz) > 0.995:  # looking straight down: world up is degenerate
            up = (math.cos(a), math.sin(a), 0.0)
        return cls(
            view_dir=(-cx, -cy, -cz), up=up, ortho=ortho, fov=fov, pad=pad
        )

    def basis(self) -> tuple[Array, Array, Array]:
        f = np.asarray(self.view_dir, dtype=np.float64)
        f = f / np.linalg.norm(f)
        up = np.asarray(self.up, dtype=np.float64)
        r = np.cross(f, up)
        n = np.linalg.norm(r)
        if n < 1e-9:
            raise ValueError("up vector is parallel to the view direction")
        r /= n
        u = np.cross(r, f)
        return r, u, f


@dataclass
class Projection:
    """Everything the rasteriser needs, precomputed for one camera."""

    sx: Array  # (m, 3) screen x per triangle vertex
    sy: Array  # (m, 3)
    depth: Array  # (m, 3) 0 = nearest
    facing: Array  # (m,) normal · view_dir; > 0 means facing the camera
    scale: float
    width: int
    height: int


def project(
    cam: Camera,
    tris: Array,
    normals: Array,
    width: int,
    height: int,
    cull: bool = False,
) -> Projection:
    """Project ``(m, 3, 3)`` triangles into pixel coordinates.

    Fitting is done against the whole scene, so every view of a body keeps the
    same apparent size -- callers that render several parts must pass the union
    of their triangles (see ``scene.merge``).
    """
    r, u, f = cam.basis()

    flat = tris.reshape(-1, 3)
    X = flat @ r
    Y = flat @ u
    Z = flat @ f

    if cam.ortho:
        px = X
        py = Y
        # Linear in view depth: smaller Z is nearer (see module docstring).
        d = Z
    else:
        half = math.radians(cam.fov) / 2.0
        span = float(np.linalg.norm(np.ptp(flat, axis=0))) or 1.0
        dist = span / (2.0 * math.tan(half)) + span * 0.25
        # Camera sits on the -f side, looking along +f.
        zc = Z + dist
        zc = np.maximum(zc, span * 1e-4)
        px = X / zc
        py = Y / zc
        # 1/z is affine in screen space, which is what a z-buffer needs.
        d = -1.0 / zc

    xmin, xmax = float(px.min()), float(px.max())
    ymin, ymax = float(py.min()), float(py.max())
    span_x = max(xmax - xmin, 1e-9)
    span_y = max(ymax - ymin, 1e-9)
    scale = min(width / (span_x * cam.pad), height / (span_y * cam.pad))
    cx = (xmin + xmax) / 2.0
    cy = (ymin + ymax) / 2.0

    SXf = (px - cx) * scale + width / 2.0
    SYf = -(py - cy) * scale + height / 2.0  # flip: image y grows downward

    dmin, dmax = float(d.min()), float(d.max())
    dn = (d - dmin) / max(dmax - dmin, 1e-12)

    m = tris.shape[0]
    # A front face points back at the camera, i.e. against the view direction.
    facing = np.einsum("ij,ij->i", normals, np.broadcast_to(f, normals.shape)) < 0

    return Projection(
        sx=SXf.reshape(m, 3),
        sy=SYf.reshape(m, 3),
        depth=dn.reshape(m, 3),
        facing=facing,
        scale=scale,
        width=width,
        height=height,
    )
