"""Scene assembly: filter parts, offset them, flatten to one triangle soup."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

import numpy as np

from .loader import Part
from .material import Material, assign


@dataclass
class Scene:
    """A mutable collection of parts plus their materials."""

    parts: list[Part] = field(default_factory=list)
    materials: dict[str, Material] = field(default_factory=dict)
    hidden: set[str] = field(default_factory=set)
    visible_only: set[str] | None = None
    offsets: dict[str, tuple[float, float, float]] = field(default_factory=dict)

    @classmethod
    def from_parts(cls, parts: list[Part], palette: str = "metal", overrides=None) -> "Scene":
        mats = assign([p.name for p in parts], palette=palette, overrides=overrides)
        return cls(parts=list(parts), materials=mats)

    # -- mutators (chainable) --------------------------------------------
    def hide(self, *names: str) -> "Scene":
        self.hidden.update(names)
        return self

    def only(self, *names: str) -> "Scene":
        self.visible_only = set(names)
        return self

    def explode(self, offsets: dict[str, tuple[float, float, float]]) -> "Scene":
        self.offsets.update(offsets)
        return self

    def copy(self) -> "Scene":
        return copy.deepcopy(self)

    # -- queries ---------------------------------------------------------
    def active(self) -> list[Part]:
        out = []
        for p in self.parts:
            if self.visible_only is not None and p.name not in self.visible_only:
                continue
            if p.name in self.hidden:
                continue
            out.append(p)
        return out

    def names(self) -> list[str]:
        return [p.name for p in self.parts]

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        parts = self.active() or self.parts
        if not parts:
            raise ValueError("scene is empty")
        lo = np.full(3, np.inf)
        hi = np.full(3, -np.inf)
        for p in parts:
            t = self._shifted(p)
            v = t.reshape(-1, 3)
            lo = np.minimum(lo, v.min(axis=0))
            hi = np.maximum(hi, v.max(axis=0))
        return lo, hi

    def _shifted(self, part: Part) -> np.ndarray:
        off = self.offsets.get(part.name)
        if not off:
            return part.tris
        return part.tris + np.asarray(off, dtype=np.float64)

    def flatten(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
        """Merge to (tris, normals, colors, order) with per-triangle colors."""
        tris, norms, cols, order = [], [], [], []
        for p in self.active():
            t = self._shifted(p)
            if t.shape[0] == 0:
                continue
            mat = self.materials.get(p.name) or Material((0.7, 0.7, 0.72))
            tris.append(t)
            norms.append(p.normals)
            cols.append(np.tile(np.asarray(mat.linear, dtype=np.float64), (t.shape[0], 1)))
            order.append(p.name)
        if not tris:
            raise ValueError("nothing visible to render")
        return (
            np.concatenate(tris),
            np.concatenate(norms),
            np.concatenate(cols),
            order,
        )

    def flatten_with_materials(self):
        """Like flatten(), but also returns per-triangle specular/shininess."""
        tris, norms, cols, order = [], [], [], []
        specs, shins = [], []
        for p in self.active():
            t = self._shifted(p)
            if t.shape[0] == 0:
                continue
            mat = self.materials.get(p.name) or Material((0.7, 0.7, 0.72))
            n = t.shape[0]
            tris.append(t)
            norms.append(p.normals)
            cols.append(np.tile(np.asarray(mat.linear, dtype=np.float64), (n, 1)))
            specs.append(np.full(n, mat.specular))
            shins.append(np.full(n, mat.shininess))
            order.append(p.name)
        if not tris:
            raise ValueError("nothing visible to render")
        return (
            np.concatenate(tris),
            np.concatenate(norms),
            np.concatenate(cols),
            np.concatenate(specs),
            np.concatenate(shins),
            order,
        )
