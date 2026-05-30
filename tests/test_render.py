from pathlib import Path
from remarkable_mcp import extract

FIXTURE = Path(__file__).parent / "fixtures" / "sample_v6.rm"


def test_v6_svg_render_extracts_paths():
    svg = extract._render_rm_v6_to_svg(FIXTURE)
    assert svg is not None, "renderer returned None for a valid v6 file"
    assert svg.count("<path") > 0, "no stroke paths rendered"
