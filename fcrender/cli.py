"""Command line interface."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

from . import __version__
from .camera import Camera, project
from .loader import LoadError, load
from .material import PALETTES, load_overrides
from .raster import Lighting, rasterize, shade
from .scene import Scene

# Named views, in degrees. azimuth 0 = +X, 90 = +Y; elevation 90 = top.
VIEWS: dict[str, tuple[float, float]] = {
    "hero": (-49.0, 28.0),
    "low": (-43.0, 12.0),
    "top": (-108.0, 86.0),
    "side": (-90.0, 3.0),
    "quarter": (-24.0, 16.0),
    "front": (0.0, 0.0),
    "iso": (45.0, 35.264),
    "back": (180.0, 20.0),
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fcrender",
        description="Headless still renderer for FreeCAD / STL models. "
        "No OpenGL, no X11, no GUI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  fcrender gearbox.FCStd -o shots --views hero,top,side
  fcrender part.stl --palette mono --bg none --size 2400x1600 --ss 3
  fcrender asm.FCStd --explode Cover:0,0,80 --hide Bolt1,Bolt2 --views quarter
""",
    )
    p.add_argument("input", help="model file (.FCStd needs FreeCAD, .stl does not)")
    p.add_argument("-o", "--out", default="render", help="output directory (default: render)")
    p.add_argument("--views", default="hero", help="comma-separated named views: " + ", ".join(VIEWS))
    p.add_argument("--az-el", metavar="AZ,EL", help="ad-hoc camera instead of --views, e.g. 35,20")
    p.add_argument("--size", default="1920x1080", help="WIDTHxHEIGHT (default 1920x1080)")
    p.add_argument("--ss", type=int, default=2, help="supersampling factor (default 2)")
    p.add_argument(
        "--bg",
        default="F7F4EF",
        help="background hex color, or 'none' for a transparent PNG (default F7F4EF)",
    )
    p.add_argument("--palette", default="metal", choices=sorted(PALETTES))
    p.add_argument("--materials", help="JSON file overriding per-part materials")
    p.add_argument("--hide", default="", help="comma-separated part names to hide")
    p.add_argument("--only", default="", help="comma-separated part names to keep")
    p.add_argument(
        "--explode",
        action="append",
        default=[],
        metavar="NAME:DX,DY,DZ",
        help="translate a part before rendering; repeatable",
    )
    p.add_argument("--cull", action="store_true", help="drop back faces (faster, closed solids only)")
    p.add_argument("--perspective", action="store_true", help="use a perspective camera")
    p.add_argument("--fov", type=float, default=32.0, help="vertical FOV in degrees (perspective)")
    p.add_argument("--pad", type=float, default=1.06, help="framing margin, >1 (default 1.06)")
    p.add_argument("--exposure", type=float, default=0.78, help="lighting exposure (default 0.78)")
    p.add_argument("--deflection", type=float, default=0.20, help="tessellation tolerance")
    p.add_argument("--jobs", type=int, default=1, help="render views in parallel (fork)")
    p.add_argument("--report", help="write a JSON report of what was rendered")
    p.add_argument("--prefix", default="", help="filename prefix for view images")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--version", action="version", version=f"fcrender {__version__}")
    return p


# --------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------
def _parse_size(text: str) -> tuple[int, int]:
    try:
        w, h = text.lower().split("x")
        return int(w), int(h)
    except Exception:
        raise SystemExit(f"bad --size {text!r}, expected WIDTHxHEIGHT")


def _parse_bg(text: str):
    if text.lower() in ("none", "transparent", "alpha"):
        return None
    h = text.lstrip("#")
    if len(h) != 6:
        raise SystemExit(f"bad --bg {text!r}, expected #rrggbb or 'none'")
    srgb = [int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]
    # Screen colors are sRGB; shading happens in linear space.
    return tuple(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in srgb)


def _parse_explode(items: list[str]) -> dict[str, tuple[float, float, float]]:
    out = {}
    for raw in items:
        try:
            name, vec = raw.split(":")
            dx, dy, dz = (float(v) for v in vec.split(","))
        except Exception:
            raise SystemExit(f"bad --explode {raw!r}, expected NAME:DX,DY,DZ")
        out[name] = (dx, dy, dz)
    return out


def _csv(text: str) -> list[str]:
    return [s.strip() for s in text.split(",") if s.strip()]


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def render_one(scene, cam, width, height, ss, background, lighting, cull, outdir, name) -> dict:
    from PIL import Image

    t0 = time.time()
    tris, normals, colors, specs, shins, order = scene.flatten_with_materials()
    proj = project(cam, tris, normals, width, height, cull=cull)

    # Flip normals toward the camera so thin walls and open shells stay lit
    # instead of turning black.
    _, _, f = cam.basis()
    facing = np.einsum("ij,ij->i", normals, np.broadcast_to(f, normals.shape)) < 0
    n_shade = np.where(facing[:, None], normals, -normals)

    rgb = shade(n_shade, colors, specs, shins, f, lighting)

    visible = proj.facing if cull else None
    img = rasterize(proj, rgb, width, height, ss=ss, background=background, visible=visible)

    path = os.path.join(outdir, name + ".png")
    Image.fromarray(img, "RGBA").save(path, optimize=True)

    return {
        "view": name,
        "file": path,
        "seconds": round(time.time() - t0, 2),
        "triangles": int(tris.shape[0]),
        "parts": order,
        "bytes": os.path.getsize(path),
    }


_JOB: dict = {}


def _run_named(name: str) -> dict:
    kw = dict(_JOB)
    cams = kw.pop("cams")
    return render_one(cam=cams[name], name=name, **kw)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    width, height = _parse_size(args.size)
    background = _parse_bg(args.bg)
    os.makedirs(args.out, exist_ok=True)

    t0 = time.time()
    try:
        parts = load(args.input, deflection=args.deflection)
    except LoadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    overrides = load_overrides(args.materials) if args.materials else None
    scene = Scene.from_parts(parts, palette=args.palette, overrides=overrides)
    if args.hide:
        scene.hide(*_csv(args.hide))
    if args.only:
        scene.only(*_csv(args.only))
    if args.explode:
        scene.explode(_parse_explode(args.explode))

    lo, hi = scene.bounds()
    if not args.quiet:
        print(
            f"loaded {len(parts)} part(s), {sum(len(p) for p in parts):,} triangles, "
            f"extent {np.round(hi - lo, 2).tolist()}  ({time.time() - t0:.1f}s)"
        )

    if args.az_el:
        try:
            az, el = (float(v) for v in args.az_el.split(","))
        except Exception:
            raise SystemExit(f"bad --az-el {args.az_el!r}, expected AZ,EL")
        cams = {f"az{az:g}_el{el:g}": Camera.from_angles(az, el, ortho=not args.perspective, fov=args.fov, pad=args.pad)}
    else:
        unknown = [v for v in _csv(args.views) if v not in VIEWS]
        if unknown:
            print(f"error: unknown view(s) {unknown}; known: {sorted(VIEWS)}", file=sys.stderr)
            return 2
        cams = {}
        for v in _csv(args.views):
            az, el = VIEWS[v]
            cams[v] = Camera.from_angles(az, el, ortho=not args.perspective, fov=args.fov, pad=args.pad)

    # Prefix applies to filenames, so map prefixed names back to cameras.
    cams = {f"{args.prefix}{k}": v for k, v in cams.items()}

    _JOB.update(
        scene=scene,
        width=width,
        height=height,
        ss=args.ss,
        background=background,
        lighting=Lighting(exposure=args.exposure),
        cull=args.cull,
        outdir=args.out,
        cams=cams,
    )

    names = list(cams)
    results: list[dict] = []
    if args.jobs > 1 and len(names) > 1:
        import multiprocessing as mp

        ctx = mp.get_context("fork")
        with ctx.Pool(processes=min(args.jobs, len(names))) as pool:
            results = pool.map(_run_named, names)
        for r in results:
            if not args.quiet:
                print(f"  {r['view']:<16} {r['seconds']:>6.2f}s  {r['bytes'] // 1024:>6} KB")
    else:
        for n in names:
            r = _run_named(n)
            results.append(r)
            if not args.quiet:
                print(f"  {r['view']:<16} {r['seconds']:>6.2f}s  {r['bytes'] // 1024:>6} KB")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "input": os.path.abspath(args.input),
                    "size": [width, height],
                    "supersample": args.ss,
                    "palette": args.palette,
                    "orthographic": not args.perspective,
                    "bbox_min": lo.tolist(),
                    "bbox_max": hi.tolist(),
                    "views": results,
                },
                fh,
                indent=2,
            )
    if not args.quiet:
        print(f"wrote {len(results)} image(s) to {os.path.abspath(args.out)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
