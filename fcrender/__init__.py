"""fcrender -- headless still renderer for FreeCAD and STL models.

Typical use, no CLI involved::

    from fcrender import load, Scene, Camera, render_view

    scene = Scene.from_parts(load("gearbox.FCStd"), palette="metal")
    cam = Camera.from_angles(azimuth=-49, elevation=28)
    render_view(scene, cam, "hero.png")
"""

from .camera import Camera, Projection, project
from .loader import LoadError, Part, load, load_fcstd, load_stl
from .material import Material, PALETTES, assign, load_overrides
from .raster import Lighting, Light, rasterize, shade
from .scene import Scene

__version__ = "0.1.0"

__all__ = [
    "Camera",
    "Light",
    "Lighting",
    "LoadError",
    "Material",
    "PALETTES",
    "Part",
    "Projection",
    "Scene",
    "assign",
    "load",
    "load_fcstd",
    "load_overrides",
    "load_stl",
    "project",
    "rasterize",
    "render_view",
    "shade",
]


def render_view(
    scene: Scene,
    cam: Camera,
    path: str,
    width: int = 1920,
    height: int = 1080,
    ss: int = 2,
    background: tuple[float, float, float] | None = (0.965, 0.957, 0.941),
    lighting: Lighting | None = None,
    cull: bool = False,
) -> str:
    """Render one view and write a PNG. Returns the path."""
    from .cli import render_one

    outdir, name = path.rsplit("/", 1) if "/" in path else (".", path)
    if name.endswith(".png"):
        name = name[:-4]
    return render_one(
        scene=scene,
        cam=cam,
        width=width,
        height=height,
        ss=ss,
        background=background,
        lighting=lighting or Lighting(),
        cull=cull,
        outdir=outdir,
        name=name,
    )["file"]
