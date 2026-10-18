"""Smoke tests. Deliberately FreeCAD-free: everything here runs on numpy+Pillow.

The .FCStd path is exercised by the example in README, not by CI, because
FreeCAD's Python bindings are not pip-installable.
"""
import struct

import numpy as np
import pytest

from fcrender import Camera, Lighting, Scene, load_stl, project, rasterize, shade
from fcrender.camera import Projection
from fcrender.material import assign, load_overrides, srgb_to_linear


CUBE_TRIS = np.array(
    [
        # +X
        [[1, 0, 0], [1, 1, 0], [1, 1, 1]],
        [[1, 0, 0], [1, 1, 1], [1, 0, 1]],
        # -X
        [[0, 0, 0], [0, 0, 1], [0, 1, 1]],
        [[0, 0, 0], [0, 1, 1], [0, 1, 0]],
        # +Y
        [[0, 1, 0], [0, 1, 1], [1, 1, 1]],
        [[0, 1, 0], [1, 1, 1], [1, 1, 0]],
        # -Y
        [[0, 0, 0], [1, 0, 0], [1, 0, 1]],
        [[0, 0, 0], [1, 0, 1], [0, 0, 1]],
        # +Z
        [[0, 0, 1], [1, 0, 1], [1, 1, 1]],
        [[0, 0, 1], [1, 1, 1], [0, 1, 1]],
        # -Z
        [[0, 0, 0], [0, 1, 0], [1, 1, 0]],
        [[0, 0, 0], [1, 1, 0], [1, 0, 0]],
    ],
    dtype=np.float64,
)


def write_binary_stl(path, tris):
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(tris)))
        for tri in tris:
            fh.write(struct.pack("<3f", 0.0, 0.0, 0.0))
            for v in tri:
                fh.write(struct.pack("<3f", *v))
            fh.write(b"\0\0")


def write_ascii_stl(path, tris):
    lines = ["solid test"]
    for tri in tris:
        lines.append("  facet normal 0 0 0")
        lines.append("    outer loop")
        for v in tri:
            lines.append(f"      vertex {v[0]} {v[1]} {v[2]}")
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append("endsolid test")
    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
def test_binary_stl_roundtrip(tmp_path):
    p = tmp_path / "cube.stl"
    write_binary_stl(p, CUBE_TRIS)
    part = load_stl(str(p))
    assert len(part) == 12
    np.testing.assert_allclose(part.bbox[0], [0, 0, 0], atol=1e-6)
    np.testing.assert_allclose(part.bbox[1], [1, 1, 1], atol=1e-6)


def test_ascii_stl_roundtrip(tmp_path):
    p = tmp_path / "cube_ascii.stl"
    write_ascii_stl(p, CUBE_TRIS)
    part = load_stl(str(p))
    assert len(part) == 12
    np.testing.assert_allclose(part.bbox[1], [1, 1, 1], atol=1e-6)


def test_normals_are_unit_length(tmp_path):
    p = tmp_path / "cube.stl"
    write_binary_stl(p, CUBE_TRIS)
    part = load_stl(str(p))
    np.testing.assert_allclose(np.linalg.norm(part.normals, axis=1), 1.0, atol=1e-9)


def test_missing_file_is_a_load_error(tmp_path):
    from fcrender.loader import LoadError, load

    with pytest.raises((LoadError, FileNotFoundError)):
        load(str(tmp_path / "nope.stl"))


# --------------------------------------------------------------------------
# materials
# --------------------------------------------------------------------------
def test_sibling_gears_share_a_colour():
    mats = assign(["SunGear", "PlanetGear_1", "PlanetGear_2", "PlanetGear_3", "RingGear", "Cover"])
    assert mats["PlanetGear_1"] == mats["PlanetGear_2"] == mats["PlanetGear_3"]
    # sun and ring must not collide with the planets
    assert mats["SunGear"] != mats["RingGear"]
    assert mats["RingGear"] != mats["PlanetGear_1"]


def test_unknown_names_still_get_a_material():
    mats = assign(["Thing1", "Thing2", "Thing3"])
    assert len(mats) == 3
    assert all(m.specular > 0 for m in mats.values())


def test_overrides_win(tmp_path):
    cfg = tmp_path / "mats.json"
    cfg.write_text('{"SunGear": {"color": "ff0000", "shininess": 90}}', encoding="utf-8")
    over = load_overrides(str(cfg))
    mats = assign(["SunGear", "Cover"], overrides=over)
    assert mats["SunGear"].shininess == 90
    assert mats["SunGear"].color == (1.0, 0.0, 0.0)


def test_srgb_to_linear_endpoints():
    assert srgb_to_linear(0.0) == 0.0
    assert abs(srgb_to_linear(1.0) - 1.0) < 1e-9
    assert srgb_to_linear(0.5) == pytest.approx(0.2140, abs=1e-3)


# --------------------------------------------------------------------------
# camera / raster
# --------------------------------------------------------------------------
def _projection(cam=None, cull=False):
    cam = cam or Camera.from_angles(35, 25)
    normals = np.cross(
        CUBE_TRIS[:, 1] - CUBE_TRIS[:, 0], CUBE_TRIS[:, 2] - CUBE_TRIS[:, 0]
    )
    normals /= np.linalg.norm(normals, axis=1, keepdims=True)
    return cam, project(cam, CUBE_TRIS, normals, 200, 200, cull=cull), normals


def test_projection_fits_inside_the_frame():
    _, proj, _ = _projection()
    assert proj.sx.min() >= -1 and proj.sx.max() <= 201
    assert proj.sy.min() >= -1 and proj.sy.max() <= 201


def test_depth_is_normalised_zero_to_one():
    _, proj, _ = _projection()
    assert proj.depth.min() >= -1e-9
    assert proj.depth.max() <= 1 + 1e-9


def test_perspective_keeps_everything_on_screen():
    cam = Camera.from_angles(35, 25, ortho=False, fov=40)
    _, proj, _ = _projection(cam)
    assert np.isfinite(proj.sx).all()
    assert proj.sx.min() > 0 and proj.sx.max() < 200


def test_render_produces_opaque_pixels():
    cam, proj, normals = _projection()
    _, _, f = cam.basis()
    facing = np.einsum("ij,ij->i", normals, np.broadcast_to(f, normals.shape)) < 0
    n = np.where(facing[:, None], normals, -normals)
    base = np.tile([0.5, 0.5, 0.5], (len(normals), 1))
    spec = np.full(len(normals), 0.6)
    shin = np.full(len(normals), 50.0)
    rgb = shade(n, base, spec, shin, f, Lighting())
    img = rasterize(proj, rgb, 200, 200, ss=1, background=None)
    assert img.shape == (200, 200, 4)
    assert img.dtype == np.uint8
    coverage = (img[..., 3] > 0).mean()
    assert 0.05 < coverage < 0.95  # the cube lands somewhere in the middle


def test_transparent_background_stays_transparent():
    cam, proj, normals = _projection()
    rgb = np.tile([0.5, 0.5, 0.5], (len(normals), 1))
    img = rasterize(proj, rgb, 120, 120, ss=1, background=None)
    assert img[0, 0, 3] == 0
    assert img[60, 60, 3] == 255


def test_culling_keeps_the_silhouette():
    cam, full, _ = _projection()
    _, culled, _ = _projection(cam, cull=True)
    a = rasterize(full, np.tile([0.5, 0.5, 0.5], (12, 1)), 160, 160, ss=1, background=None)
    b = rasterize(
        culled,
        np.tile([0.5, 0.5, 0.5], (12, 1)),
        160,
        160,
        ss=1,
        background=None,
        visible=culled.facing,
    )
    assert (a[..., 3] > 0).sum() == pytest.approx((b[..., 3] > 0).sum(), rel=0.02)


# --------------------------------------------------------------------------
# scene
# --------------------------------------------------------------------------
def test_scene_hide_and_only(tmp_path):
    p = tmp_path / "cube.stl"
    write_binary_stl(p, CUBE_TRIS)
    from fcrender import load

    scene = Scene.from_parts(load(str(p)))
    assert len(scene.active()) == 1
    scene.hide("cube")
    assert scene.active() == []


def test_explode_shifts_bounds():
    from fcrender.loader import Part

    part = Part("A", CUBE_TRIS, np.zeros((12, 3)))
    scene = Scene.from_parts([part])
    lo0, hi0 = scene.bounds()
    scene.explode({"A": (10, 0, 0)})
    lo1, hi1 = scene.bounds()
    np.testing.assert_allclose(lo1 - lo0, [10, 0, 0])
    np.testing.assert_allclose(hi1 - hi0, [10, 0, 0])
