"""
Text extraction helpers for reMarkable documents.
"""

import json
import tempfile
import zipfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional


def find_similar_documents(query: str, documents: List, limit: int = 5) -> List[str]:
    """Find documents with similar names for 'did you mean' suggestions."""
    query_lower = query.lower()
    scored = []
    for doc in documents:
        name = doc.VissibleName
        # Use sequence matcher for fuzzy matching
        ratio = SequenceMatcher(None, query_lower, name.lower()).ratio()
        # Boost partial matches
        if query_lower in name.lower():
            ratio += 0.3
        scored.append((name, ratio))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [name for name, score in scored[:limit] if score > 0.3]


def extract_text_from_rm_file(rm_file_path: Path) -> List[str]:
    """
    Extract typed text from a .rm file using rmscene.

    This extracts text that was typed via Type Folio or on-screen keyboard.
    Does NOT require OCR - text is stored natively in v6 .rm files.
    """
    try:
        from rmscene import read_blocks
        from rmscene.scene_items import Text
        from rmscene.scene_tree import SceneTree

        with open(rm_file_path, "rb") as f:
            tree = SceneTree()
            for block in read_blocks(f):
                tree.add_block(block)

        text_lines = []

        # Extract text from the scene tree
        for item in tree.root.children.values():
            if hasattr(item, "value") and isinstance(item.value, Text):
                text_obj = item.value
                if hasattr(text_obj, "items"):
                    for text_item in text_obj.items:
                        if hasattr(text_item, "value") and text_item.value:
                            text_lines.append(str(text_item.value))

        return text_lines

    except ImportError:
        return []  # rmscene not available
    except Exception:
        # Log but don't fail - file might be older format
        return []


def extract_text_from_document_zip(zip_path: Path, include_ocr: bool = False) -> Dict[str, Any]:
    """
    Extract all text content from a reMarkable document zip.

    Returns:
        {
            "typed_text": [...],      # From rmscene parsing
            "highlights": [...],       # From PDF annotations
            "handwritten_text": [...], # From OCR (if enabled)
            "pages": int
        }
    """
    result: Dict[str, Any] = {
        "typed_text": [],
        "highlights": [],
        "handwritten_text": None,
        "pages": 0,
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        rm_files = list(tmpdir_path.glob("**/*.rm"))
        result["pages"] = len(rm_files)

        # Extract typed text from .rm files using rmscene
        for rm_file in rm_files:
            text_lines = extract_text_from_rm_file(rm_file)
            result["typed_text"].extend(text_lines)

        # Extract text from .txt and .md files
        for txt_file in tmpdir_path.glob("**/*.txt"):
            try:
                content = txt_file.read_text(errors="ignore")
                if content.strip():
                    result["typed_text"].append(content)
            except Exception:
                pass

        for md_file in tmpdir_path.glob("**/*.md"):
            try:
                content = md_file.read_text(errors="ignore")
                if content.strip():
                    result["typed_text"].append(content)
            except Exception:
                pass

        # Extract from .content files (metadata with text)
        for content_file in tmpdir_path.glob("**/*.content"):
            try:
                data = json.loads(content_file.read_text())
                if "text" in data:
                    result["typed_text"].append(data["text"])
            except Exception:
                pass

        # Extract PDF highlights
        for json_file in tmpdir_path.glob("**/*.json"):
            try:
                data = json.loads(json_file.read_text())
                if isinstance(data, dict) and "highlights" in data:
                    for h in data.get("highlights", []):
                        if "text" in h and h["text"]:
                            result["highlights"].append(h["text"])
            except Exception:
                pass

        # OCR for handwritten content (optional)
        if include_ocr and rm_files:
            result["handwritten_text"] = extract_handwriting_ocr(rm_files)

    return result


def extract_handwriting_ocr(rm_files: List[Path]) -> Optional[List[str]]:
    """
    Extract handwritten text using OCR.
    Requires optional OCR dependencies (pytesseract, rmc, cairosvg).
    """
    try:
        import subprocess

        import pytesseract
        from PIL import Image

        ocr_results = []

        for rm_file in rm_files:
            tmp_svg_path = None
            tmp_png_path = None
            try:
                # Create temp files
                with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
                    tmp_svg_path = Path(tmp_svg.name)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_png:
                    tmp_png_path = Path(tmp_png.name)

                # Convert .rm to SVG using rmc
                result = subprocess.run(
                    ["rmc", "-t", "svg", "-o", str(tmp_svg_path), str(rm_file)],
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    continue

                # Convert SVG to PNG using cairosvg or inkscape
                try:
                    import cairosvg

                    cairosvg.svg2png(
                        url=str(tmp_svg_path),
                        write_to=str(tmp_png_path),
                        output_width=1404,  # reMarkable width
                        output_height=1872,  # reMarkable height
                    )
                except ImportError:
                    # Fall back to inkscape if cairosvg not available
                    result = subprocess.run(
                        [
                            "inkscape",
                            str(tmp_svg_path),
                            "--export-filename",
                            str(tmp_png_path),
                        ],
                        capture_output=True,
                        timeout=30,
                    )
                    if result.returncode != 0:
                        continue

                # Run OCR on the PNG
                img = Image.open(tmp_png_path)
                text = pytesseract.image_to_string(img)
                if text.strip():
                    ocr_results.append(text.strip())

            except subprocess.TimeoutExpired:
                pass
            except FileNotFoundError:
                # rmc not installed
                return None
            finally:
                # Clean up temp files
                if tmp_svg_path:
                    tmp_svg_path.unlink(missing_ok=True)
                if tmp_png_path:
                    tmp_png_path.unlink(missing_ok=True)

        return ocr_results if ocr_results else None

    except ImportError:
        # OCR dependencies not installed
        return None
