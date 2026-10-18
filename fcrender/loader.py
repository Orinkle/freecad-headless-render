"""Load a CAD file into flat triangle soup.

Two backends:

* ``.FCStd`` -> FreeCAD's own tessellator (imports ``FreeCAD``, so needs a
  FreeCAD Python install).
* ``.stl``   -> a 60-line parser, pure numpy. No FreeCAD required.

The STL path is the reason this tool is usable by people who do not have
FreeCAD: every CAD package on earth exports STL.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field

import numpy as np


class LoadError(RuntimeError):
    pass


@dataclass
class Part:
    """One solid. Triangles are already in world space, wound outward."""

    name: str
    tris: np.ndarray  # (n, 3, 3) float64
    normals: np.ndarray  # (n, 3) float64, unit length
    source: str = ""
    meta: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return int(self.tris.shape[0])

    @property
    def bbox(self) -> tuple[np.ndarray, np.ndarray]:
        v = self.tris.reshape(-1, 3)
        return v.min(axis=0), v.max(axis=0)


def _tri_normals(tris: np.ndarray) -> np.ndarray:
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    ln = np.linalg.norm(n, axis=1)
    keep = ln > 1e-12
    n[~keep] = 0.0
    ln[~keep] = 1.0
    return n / ln[:, None]


def load(path: str, deflection: float = 0.20, **kwargs) -> list[Part]:
    """Dispatch on extension."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".fcstd":
        return load_fcstd(path, deflection=deflection, **kwargs)
    if ext == ".stl":
        return [load_stl(path)]
    raise LoadError(
        f"unsupported input {ext!r}. Supported: .FCStd (needs FreeCAD), .stl"
    )


# --------------------------------------------------------------------------
# STL
# --------------------------------------------------------------------------
def load_stl(path: str, name: str | None = None) -> Part:
    """Binary or ASCII STL -> Part. Auto-detected, because exporters lie about
    the format roughly 10% of the time."""
    with open(path, "rb") as fh:
        head = fh.read(84)
        if len(head) < 84:
            raise LoadError(f"{path}: file too short to be an STL")
        n = struct.unpack("<I", head[80:84])[0]
        expected = 84 + n * 50
        actual = os.path.getsize(path)
        binary = expected == actual

        if binary:
            fh.seek(84)
            buf = np.fromfile(fh, dtype=np.uint8)
            if buf.size < n * 50:
                raise LoadError(f"{path}: truncated binary STL")
            rec = buf.reshape(n, 50)
            # 12 float32 per facet: normal(3) + 3 vertices(3 each), then 2 attr bytes
            data = rec[:, :48].copy().view("<f4").reshape(n, 12).astype(np.float64)
            tris = data[:, 3:].reshape(n, 3, 3)
            stored = data[:, :3]
        else:
            fh.seek(0)
            text = fh.read().decode("utf-8", errors="replace")
            verts = _parse_ascii_vertices(text)
            if verts.size == 0:
                raise LoadError(f"{path}: not a readable STL (neither binary nor ASCII)")
            tris = verts.reshape(-1, 3, 3)
            stored = None

    if tris.size == 0:
        raise LoadError(f"{path}: STL contains no facets")

    normals = _tri_normals(tris)
    # Trust the file's normals only when they agree with the winding; exporters
    # frequently write (0,0,0) or garbage there.
    if stored is not None:
        ok = np.linalg.norm(stored, axis=1) > 1e-9
        agree = np.einsum("ij,ij->i", stored[ok], normals[ok]) > 0.5
        if ok.sum() and agree.mean() < 0.5:
            normals[ok] = stored[ok] / np.linalg.norm(stored[ok], axis=1, keepdims=True)

    return Part(
        name=name or os.path.splitext(os.path.basename(path))[0],
        tris=tris,
        normals=normals,
        source=path,
        meta={"format": "stl", "binary": bool(binary)},
    )


def _parse_ascii_vertices(text: str) -> np.ndarray:
    """Pull every `vertex x y z` triple out of an ASCII STL."""
    out: list[float] = []
    for line in text.splitlines():
        s = line.strip()
        if not s.lower().startswith("vertex"):
            continue
        parts = s.split()
        if len(parts) != 4:
            continue
        try:
            out.extend((float(parts[1]), float(parts[2]), float(parts[3])))
        except ValueError:
            continue
    return np.asarray(out, dtype=np.float64)


# --------------------------------------------------------------------------
# FCStd
# --------------------------------------------------------------------------
def load_fcstd(
    path: str,
    deflection: float = 0.20,
    include=None,
) -> list[Part]:
    """Tessellate every visible solid in a FreeCAD document.

    Requires ``import FreeCAD`` to work. When FreeCAD lives outside the
    interpreter (the usual conda case) prepend its ``lib`` directory to
    ``PYTHONPATH`` -- see README.
    """
    try:
        import FreeCAD as App  # noqa: WPS433  (deliberately lazy)
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise LoadError(
            "reading .FCStd needs FreeCAD's Python bindings, compiled for one\n"
            "specific interpreter version -- they will not load into another.\n"
            f"  underlying error: {exc}\n"
            "  Run fcrender with FreeCAD's own interpreter instead:\n"
            "    PYTHONPATH=<freecad>/lib:<repo> <freecad>/bin/python -m fcrender.cli model.FCStd\n"
            "  Or export STL from your CAD tool and render that: no FreeCAD needed."
        ) from exc

    doc = App.openDocument(path)
    doc.recompute()

    parts: list[Part] = []
    for obj in doc.Objects:
        shape = getattr(obj, "Shape", None)
        if shape is None or shape.isNull():
            continue
        if include and obj.Name not in include:
            continue
        try:
            # Second positional arg of Shape.tessellate is a bool, not an
            # angular tolerance -- pass only the linear deflection.
            verts, facets = shape.tessellate(deflection)
        except Exception:  # empty / degenerate solids
            continue
        if not facets:
            continue

        v = np.array([[p.x, p.y, p.z] for p in verts], dtype=np.float64)
        f = np.asarray(facets, dtype=np.int64)

        # Placement is already baked into `shape` (Shape is world-space for
        # simple Part::Feature objects). Applying Placement again would
        # double-offset the part -- this bug cost an afternoon once.
        tris = v[f]
        parts.append(
            Part(
                name=obj.Name,
                tris=tris,
                normals=_tri_normals(tris),
                source=path,
                meta={
                    "format": "fcstd",
                    "label": getattr(obj, "Label", obj.Name),
                    "type": obj.TypeId,
                    "faces": len(shape.Faces),
                },
            )
        )
    if not parts:
        raise LoadError(f"{path}: no solid geometry found")
    return parts
