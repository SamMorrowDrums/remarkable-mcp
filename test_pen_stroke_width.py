"""Pen stroke width follows Point.width instead of saturating a 5.0 clamp (#189)."""

from __future__ import annotations

import re
from types import SimpleNamespace

from remarkable_mcp import extract


def _pt(x, y, width):
    return SimpleNamespace(x=x, y=y, width=width, pressure=1000, speed=0, direction=0)


def _line_block(points, tool=17, color=0):
    line = SimpleNamespace(points=points, tool=tool, color=color)
    return SimpleNamespace(item=SimpleNamespace(value=line), parent_id=None)


def _widths(paths):
    return [float(m.group(1)) for p in paths for m in re.finditer(r'stroke-width="([\d.]+)"', p)]


def test_raw_width_maps_to_scene_units_not_a_saturated_clamp():
    # Fixed point x4: a raw 8 is a 2.0-unit tip; a raw 12 is 3.0. Both used to be 5.0.
    assert extract._pen_width_units(8) == 2.0
    assert extract._pen_width_units(12) == 3.0
    assert extract._pen_width_units(1) == extract.PEN_WIDTH_MIN
    assert extract._pen_width_units(1000) == extract.PEN_WIDTH_MAX


def test_uniform_stroke_is_a_single_path():
    paths = extract._variable_width_pen_paths([(0, 0, 8), (10, 0, 8), (20, 0, 8)], "black")
    assert len(paths) == 1
    assert _widths(paths) == [2.0]
    assert 'd="M 0.0 0.0 L 10.0 0.0 L 20.0 0.0"' in paths[0]


def test_pressure_change_splits_into_runs_that_overlap_by_one_point():
    pts = [(0, 0, 8), (10, 0, 8), (20, 0, 12), (30, 0, 12), (40, 0, 8)]
    paths = extract._variable_width_pen_paths(pts, "black")
    assert _widths(paths) == [2.0, 3.0, 2.0]
    # run 2 starts at the last point of run 1 so there is no gap at the join
    assert 'd="M 10.0 0.0 L 20.0 0.0 L 30.0 0.0"' in paths[1]
    assert 'd="M 30.0 0.0 L 40.0 0.0"' in paths[2]


def test_small_jitter_is_quantized_into_one_run():
    # 8.0 -> 2.0, 8.3 -> 2.075 -> rounds to 2.0 as well: no split for noise.
    paths = extract._variable_width_pen_paths([(0, 0, 8.0), (10, 0, 8.3), (20, 0, 7.8)], "black")
    assert len(paths) == 1


def test_single_point_becomes_a_dot():
    paths = extract._variable_width_pen_paths([(5, 5, 8)], "black")
    assert len(paths) == 1 and 'd="M 5.0 5.0 L 5.0 5.0"' in paths[0]


def test_v6_pen_uses_per_point_widths_and_offsets():
    blocks = [_line_block([_pt(0, 0, 8), _pt(10, 0, 8), _pt(20, 0, 12)])]
    paths, coords = extract._v6_paths_from_blocks(blocks)
    assert _widths(paths) == [2.0, 3.0]
    assert coords == [(0, 0), (10, 0), (20, 0)]
    assert 5.0 not in _widths(paths)


def test_v6_highlighter_keeps_its_own_width_rule():
    blocks = [_line_block([_pt(0, 0, 10), _pt(10, 0, 10)], tool=5)]
    paths, _ = extract._v6_paths_from_blocks(blocks)
    assert len(paths) == 1 and 'stroke-width="20.0"' in paths[0] and 'opacity="0.35"' in paths[0]


def test_v6_missing_width_falls_back_to_a_fineliner():
    pts = [SimpleNamespace(x=0, y=0), SimpleNamespace(x=10, y=0)]
    blocks = [_line_block(pts)]
    paths, _ = extract._v6_paths_from_blocks(blocks)
    assert _widths(paths) == [2.0]
