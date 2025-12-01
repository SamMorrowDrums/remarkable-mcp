"""
Text extraction helpers for reMarkable documents.
"""

import json
import tempfile
import zipfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional

# reMarkable tablet screen dimensions (in pixels) - used as fallback
REMARKABLE_WIDTH = 1404
REMARKABLE_HEIGHT = 1872

# Standard reMarkable background color (light cream/gray)
REMARKABLE_BACKGROUND_COLOR = "#FBFBFB"

# Margin around content when using content-based bounding box (in pixels)
CONTENT_MARGIN = 50

# Module-level cache for OCR results
# Key: doc_id, Value: {"result": extraction_result, "include_ocr": bool}
_extraction_cache: Dict[str, Dict[str, Any]] = {}


def clear_extraction_cache(doc_id: Optional[str] = None) -> None:
    """
    Clear the extraction cache.

    Args:
        doc_id: If provided, only clear cache for this document.
                If None, clear the entire cache.
    """
    if doc_id:
        _extraction_cache.pop(doc_id, None)
    else:
        _extraction_cache.clear()


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


def extract_text_from_pdf(pdf_path: Path) -> str:
    """
    Extract text from a PDF file using PyMuPDF.

    Returns the full text content of the PDF.
    """
    try:
        import fitz  # PyMuPDF

        text_parts = []
        with fitz.open(pdf_path) as doc:
            for page_num, page in enumerate(doc, 1):
                page_text = page.get_text()
                if page_text.strip():
                    text_parts.append(f"--- Page {page_num} ---\n{page_text.strip()}")

        return "\n\n".join(text_parts) if text_parts else ""
    except ImportError:
        return ""
    except Exception:
        return ""


def extract_text_from_epub(epub_path: Path) -> str:
    """
    Extract text from an EPUB file.

    Returns the full text content of the EPUB.
    """
    try:
        from bs4 import BeautifulSoup
        from ebooklib import ITEM_DOCUMENT, epub

        book = epub.read_epub(str(epub_path), options={"ignore_ncx": True})
        text_parts = []

        for item in book.get_items():
            if item.get_type() == ITEM_DOCUMENT:
                soup = BeautifulSoup(item.get_content(), "html.parser")
                # Get text, preserving some structure
                text = soup.get_text(separator="\n", strip=True)
                if text:
                    text_parts.append(text)

        return "\n\n".join(text_parts) if text_parts else ""
    except ImportError:
        return ""
    except Exception:
        return ""


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


def _get_svg_content_bounds(svg_path: Path) -> Optional[tuple]:
    """
    Parse SVG file to get the content bounding box from viewBox.

    Args:
        svg_path: Path to the SVG file

    Returns:
        Tuple of (min_x, min_y, width, height) or None if not determinable
    """
    import xml.etree.ElementTree as ET

    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()

        # Try to get viewBox attribute
        viewbox = root.get("viewBox")
        if viewbox:
            parts = viewbox.split()
            if len(parts) == 4:
                return (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))

        # Fallback to width/height attributes
        width = root.get("width")
        height = root.get("height")
        if width and height:
            # Remove 'px' suffix if present
            w = float(width.replace("px", ""))
            h = float(height.replace("px", ""))
            return (0, 0, w, h)

        return None
    except Exception:
        return None


def render_rm_file_to_png(
    rm_file_path: Path, background_color: Optional[str] = None
) -> Optional[bytes]:
    """
    Render a .rm file to PNG image bytes.

    Uses rmc to convert .rm to SVG, then cairosvg to convert to PNG.
    The output is sized based on the SVG content bounds with a margin.

    Args:
        rm_file_path: Path to the .rm file
        background_color: Background color (e.g., "#FFFFFF", "transparent", None).
                         None means transparent. Use REMARKABLE_BACKGROUND_COLOR
                         for the standard reMarkable paper color.

    Returns:
        PNG image bytes, or None if rendering failed
    """
    import subprocess
    import tempfile

    tmp_svg_path = None
    tmp_png_path = None
    tmp_raw_path = None

    try:
        # Create temp files
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
            tmp_svg_path = Path(tmp_svg.name)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_png:
            tmp_png_path = Path(tmp_png.name)

        # Convert .rm to SVG using rmc
        result = subprocess.run(
            ["rmc", "-t", "svg", "-o", str(tmp_svg_path), str(rm_file_path)],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None

        # Get content bounds from SVG
        bounds = _get_svg_content_bounds(tmp_svg_path)
        if bounds:
            # Use content bounds with margin
            _, _, content_width, content_height = bounds
            output_width = int(content_width) + 2 * CONTENT_MARGIN
            output_height = int(content_height) + 2 * CONTENT_MARGIN
        else:
            # Fallback to standard reMarkable dimensions
            output_width = REMARKABLE_WIDTH
            output_height = REMARKABLE_HEIGHT

        # Convert SVG to PNG
        try:
            import cairosvg
            from PIL import Image as PILImage

            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_raw:
                tmp_raw_path = Path(tmp_raw.name)

            # Use cairosvg with background_color if specified
            cairosvg.svg2png(
                url=str(tmp_svg_path),
                write_to=str(tmp_raw_path),
                output_width=output_width,
                output_height=output_height,
                background_color=background_color,
            )

            # If no background color specified (transparent), return as-is
            if background_color is None:
                with open(tmp_raw_path, "rb") as f:
                    return f.read()

            # If background color specified, ensure it's applied properly
            img = PILImage.open(tmp_raw_path)
            if img.mode == "RGBA" and background_color:
                # Parse hex color
                if background_color.startswith("#"):
                    hex_color = background_color.lstrip("#")
                    if len(hex_color) == 6:
                        r, g, b = tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
                    else:
                        r, g, b = 255, 255, 255
                else:
                    r, g, b = 255, 255, 255
                bg = PILImage.new("RGB", img.size, (r, g, b))
                bg.paste(img, mask=img.split()[3])
                img = bg
            img.save(tmp_png_path)

            with open(tmp_png_path, "rb") as f:
                return f.read()

        except ImportError:
            # Fall back to inkscape
            result = subprocess.run(
                ["inkscape", str(tmp_svg_path), "--export-filename", str(tmp_png_path)],
                capture_output=True,
                timeout=30,
            )
            if result.returncode != 0:
                return None

            with open(tmp_png_path, "rb") as f:
                return f.read()

    except subprocess.TimeoutExpired:
        return None
    except FileNotFoundError:
        # rmc not installed
        return None
    except Exception:
        return None
    finally:
        if tmp_svg_path:
            tmp_svg_path.unlink(missing_ok=True)
        if tmp_png_path:
            tmp_png_path.unlink(missing_ok=True)
        if tmp_raw_path:
            tmp_raw_path.unlink(missing_ok=True)


def render_page_from_document_zip(
    zip_path: Path, page: int = 1, background_color: Optional[str] = None
) -> Optional[bytes]:
    """
    Render a specific page from a reMarkable document zip to PNG.

    Args:
        zip_path: Path to the document zip file
        page: Page number (1-indexed)
        background_color: Background color (e.g., "#FFFFFF", None for transparent).
                         Use REMARKABLE_BACKGROUND_COLOR for the standard paper color.

    Returns:
        PNG image bytes, or None if rendering failed or page doesn't exist
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        # Get page order from .content file
        page_order = []
        for content_file in tmpdir_path.glob("*.content"):
            try:
                data = json.loads(content_file.read_text())
                # New format: cPages.pages array
                if "cPages" in data and "pages" in data["cPages"]:
                    page_order = [p["id"] for p in data["cPages"]["pages"]]
                # Fallback: pages array directly
                elif "pages" in data and isinstance(data["pages"], list):
                    page_order = data["pages"]
            except Exception:
                pass
            break

        rm_files = list(tmpdir_path.glob("**/*.rm"))

        # Sort rm_files by page order if available
        if page_order:
            rm_by_id = {}
            for rm_file in rm_files:
                page_id = rm_file.stem
                rm_by_id[page_id] = rm_file

            ordered_rm_files = []
            for page_id in page_order:
                if page_id in rm_by_id:
                    ordered_rm_files.append(rm_by_id[page_id])
            # Add any remaining files not in page order
            for rm_file in rm_files:
                if rm_file not in ordered_rm_files:
                    ordered_rm_files.append(rm_file)
            rm_files = ordered_rm_files

        # Validate page number
        if page < 1 or page > len(rm_files):
            return None

        # Render the requested page
        target_rm_file = rm_files[page - 1]
        return render_rm_file_to_png(target_rm_file, background_color=background_color)


def get_document_page_count(zip_path: Path) -> int:
    """
    Get the number of pages in a reMarkable document zip.

    Args:
        zip_path: Path to the document zip file

    Returns:
        Number of pages (0 if unable to determine)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        return len(list(tmpdir_path.glob("**/*.rm")))


def extract_text_from_document_zip(
    zip_path: Path, include_ocr: bool = False, doc_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Extract all text content from a reMarkable document zip.

    Args:
        zip_path: Path to the document zip file
        include_ocr: Whether to run OCR on handwritten content
        doc_id: Optional document ID for caching OCR results

    Returns:
        {
            "typed_text": [...],      # From rmscene parsing (list of strings)
            "highlights": [...],       # From PDF annotations
            "handwritten_text": [...], # From OCR (if enabled) - one per page, in order
            "pages": int,
            "page_ids": [...],         # Page UUIDs in order
        }
    """
    # Check cache if doc_id provided
    if doc_id and doc_id in _extraction_cache:
        cached = _extraction_cache[doc_id]
        # Return cached result if OCR requirement is satisfied
        # (cached with OCR can satisfy no-OCR request, but not vice versa)
        if cached["include_ocr"] or not include_ocr:
            return cached["result"]

    result: Dict[str, Any] = {
        "typed_text": [],
        "highlights": [],
        "handwritten_text": None,
        "pages": 0,
        "page_ids": [],
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        # Get page order from .content file
        page_order = []
        for content_file in tmpdir_path.glob("*.content"):
            try:
                data = json.loads(content_file.read_text())
                # New format: cPages.pages array
                if "cPages" in data and "pages" in data["cPages"]:
                    page_order = [p["id"] for p in data["cPages"]["pages"]]
                # Fallback: pages array directly
                elif "pages" in data and isinstance(data["pages"], list):
                    page_order = data["pages"]
            except Exception:
                pass
            break  # Only process first .content file

        rm_files = list(tmpdir_path.glob("**/*.rm"))

        # If we have page order, sort rm_files accordingly
        if page_order:
            # Create mapping of page_id -> rm_file
            rm_by_id = {}
            for rm_file in rm_files:
                page_id = rm_file.stem  # filename without extension
                rm_by_id[page_id] = rm_file

            # Sort rm_files by page order
            ordered_rm_files = []
            for page_id in page_order:
                if page_id in rm_by_id:
                    ordered_rm_files.append(rm_by_id[page_id])
            # Add any remaining files not in page order
            for rm_file in rm_files:
                if rm_file not in ordered_rm_files:
                    ordered_rm_files.append(rm_file)
            rm_files = ordered_rm_files
            result["page_ids"] = [f.stem for f in rm_files]

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

    # Cache result if doc_id provided
    if doc_id:
        _extraction_cache[doc_id] = {"result": result, "include_ocr": include_ocr}

    return result


def extract_handwriting_ocr(rm_files: List[Path]) -> Optional[List[str]]:
    """
    Extract handwritten text using OCR.

    Supports multiple backends (set REMARKABLE_OCR_BACKEND env var):
    - "google" (default if API key provided): Google Cloud Vision - best for handwriting
    - "tesseract": pytesseract - basic OCR, requires rmc + cairosvg

    Google Vision can be enabled by setting GOOGLE_VISION_API_KEY env var.
    """
    import os

    backend = os.environ.get("REMARKABLE_OCR_BACKEND", "auto").lower()

    # Auto-detect best available backend
    if backend == "auto":
        # Check for Google Vision API key first (simplest auth method)
        if os.environ.get("GOOGLE_VISION_API_KEY"):
            backend = "google"
        else:
            backend = "tesseract"

    if backend == "google":
        return _ocr_google_vision(rm_files)
    else:
        return _ocr_tesseract(rm_files)


def _ocr_google_vision(rm_files: List[Path]) -> Optional[List[str]]:
    """
    OCR using Google Cloud Vision API.
    Best quality for handwriting recognition.

    Supports two authentication methods:
    1. GOOGLE_VISION_API_KEY env var (simplest - just an API key)
    2. GOOGLE_APPLICATION_CREDENTIALS or default credentials (service account)
    """
    import os

    api_key = os.environ.get("GOOGLE_VISION_API_KEY")

    if api_key:
        # Use REST API with API key (simpler, no SDK needed)
        return _ocr_google_vision_rest(rm_files, api_key)
    else:
        # Use SDK with service account credentials
        return _ocr_google_vision_sdk(rm_files)


def _ocr_google_vision_rest(rm_files: List[Path], api_key: str) -> Optional[List[str]]:
    """
    OCR using Google Cloud Vision REST API with API key.
    """
    import base64
    import subprocess
    import tempfile

    import requests

    ocr_results = []

    for rm_file in rm_files:
        tmp_svg_path = None
        tmp_png_path = None
        tmp_raw_path = None
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

            # Convert SVG to PNG
            try:
                import cairosvg
                from PIL import Image as PILImage

                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_raw:
                    tmp_raw_path = Path(tmp_raw.name)

                cairosvg.svg2png(
                    url=str(tmp_svg_path),
                    write_to=str(tmp_raw_path),
                    output_width=REMARKABLE_WIDTH,
                    output_height=REMARKABLE_HEIGHT,
                )

                # Add white background
                img = PILImage.open(tmp_raw_path)
                if img.mode == "RGBA":
                    bg = PILImage.new("RGB", img.size, (255, 255, 255))
                    bg.paste(img, mask=img.split()[3])
                    img = bg
                img.save(tmp_png_path)
                tmp_raw_path.unlink(missing_ok=True)
                tmp_raw_path = None
            except ImportError:
                result = subprocess.run(
                    ["inkscape", str(tmp_svg_path), "--export-filename", str(tmp_png_path)],
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    continue

            # Read and encode image
            with open(tmp_png_path, "rb") as f:
                image_content = base64.b64encode(f.read()).decode("utf-8")

            # Call Google Vision REST API
            url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
            payload = {
                "requests": [
                    {
                        "image": {"content": image_content},
                        "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                    }
                ]
            }

            response = requests.post(url, json=payload, timeout=60)
            if response.status_code == 200:
                data = response.json()
                if "responses" in data and data["responses"]:
                    resp = data["responses"][0]
                    if "fullTextAnnotation" in resp:
                        text = resp["fullTextAnnotation"]["text"]
                        if text.strip():
                            ocr_results.append(text.strip())
            elif response.status_code in (401, 403):
                # API key invalid or API not enabled - fall back to Tesseract
                return _ocr_tesseract(rm_files)

        except subprocess.TimeoutExpired:
            pass
        except FileNotFoundError:
            return None
        except Exception:
            pass
        finally:
            if tmp_svg_path:
                tmp_svg_path.unlink(missing_ok=True)
            if tmp_png_path:
                tmp_png_path.unlink(missing_ok=True)
            if tmp_raw_path:
                tmp_raw_path.unlink(missing_ok=True)

    return ocr_results if ocr_results else None


def _ocr_google_vision_sdk(rm_files: List[Path]) -> Optional[List[str]]:
    """
    OCR using Google Cloud Vision SDK with service account credentials.
    """
    try:
        import subprocess
        import tempfile

        from google.cloud import vision

        client = vision.ImageAnnotatorClient()
        ocr_results = []

        for rm_file in rm_files:
            tmp_svg_path = None
            tmp_png_path = None
            tmp_raw_path = None
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

                # Convert SVG to PNG using cairosvg
                try:
                    import cairosvg
                    from PIL import Image as PILImage

                    # Convert to PNG (comes out with transparent background)
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_raw:
                        tmp_raw_path = Path(tmp_raw.name)

                    cairosvg.svg2png(
                        url=str(tmp_svg_path),
                        write_to=str(tmp_raw_path),
                        output_width=REMARKABLE_WIDTH,
                        output_height=REMARKABLE_HEIGHT,
                    )

                    # Add white background (SVG renders as black-on-transparent)
                    img = PILImage.open(tmp_raw_path)
                    if img.mode == "RGBA":
                        bg = PILImage.new("RGB", img.size, (255, 255, 255))
                        bg.paste(img, mask=img.split()[3])
                        img = bg
                    img.save(tmp_png_path)
                    tmp_raw_path.unlink(missing_ok=True)
                    tmp_raw_path = None
                except ImportError:
                    # Fall back to inkscape
                    result = subprocess.run(
                        ["inkscape", str(tmp_svg_path), "--export-filename", str(tmp_png_path)],
                        capture_output=True,
                        timeout=30,
                    )
                    if result.returncode != 0:
                        continue

                # Send to Google Vision API
                with open(tmp_png_path, "rb") as f:
                    content = f.read()

                image = vision.Image(content=content)

                # Use DOCUMENT_TEXT_DETECTION for best handwriting results
                response = client.document_text_detection(image=image)

                if response.error.message:
                    continue

                if response.full_text_annotation.text:
                    ocr_results.append(response.full_text_annotation.text.strip())

            except subprocess.TimeoutExpired:
                pass
            except FileNotFoundError:
                # rmc not installed
                return None
            finally:
                if tmp_svg_path:
                    tmp_svg_path.unlink(missing_ok=True)
                if tmp_png_path:
                    tmp_png_path.unlink(missing_ok=True)
                if tmp_raw_path:
                    tmp_raw_path.unlink(missing_ok=True)

        return ocr_results if ocr_results else None

    except ImportError:
        # google-cloud-vision not installed, fall back to tesseract
        return _ocr_tesseract(rm_files)
    except Exception:
        # API error, fall back to tesseract
        return _ocr_tesseract(rm_files)


def _ocr_tesseract(rm_files: List[Path]) -> Optional[List[str]]:
    """
    OCR using Tesseract.
    Basic quality - designed for printed text, not handwriting.

    Requires: pytesseract, rmc, cairosvg (or inkscape)
    """
    try:
        import subprocess
        import tempfile

        import pytesseract
        from PIL import Image, ImageFilter, ImageOps

        ocr_results = []

        for rm_file in rm_files:
            tmp_svg_path = None
            tmp_png_path = None
            tmp_raw_path = None
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

                # Convert SVG to PNG with higher resolution for better OCR
                try:
                    import cairosvg

                    # Convert to PNG (comes out with transparent background)
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_raw:
                        tmp_raw_path = Path(tmp_raw.name)

                    # Use 1.5x resolution for better OCR (2x is too slow)
                    cairosvg.svg2png(
                        url=str(tmp_svg_path),
                        write_to=str(tmp_raw_path),
                        output_width=2106,  # 1.5x reMarkable width
                        output_height=2808,  # 1.5x reMarkable height
                    )

                    # Add white background (SVG renders as black-on-transparent)
                    img = Image.open(tmp_raw_path)
                    if img.mode == "RGBA":
                        bg = Image.new("RGB", img.size, (255, 255, 255))
                        bg.paste(img, mask=img.split()[3])
                        img = bg
                    img.save(tmp_png_path)
                    tmp_raw_path.unlink(missing_ok=True)
                    tmp_raw_path = None
                except ImportError:
                    result = subprocess.run(
                        ["inkscape", str(tmp_svg_path), "--export-filename", str(tmp_png_path)],
                        capture_output=True,
                        timeout=30,
                    )
                    if result.returncode != 0:
                        continue

                # Preprocess image for better OCR
                img = Image.open(tmp_png_path)

                # Convert to grayscale
                img = img.convert("L")

                # Increase contrast
                img = ImageOps.autocontrast(img, cutoff=2)

                # Slight sharpening
                img = img.filter(ImageFilter.SHARPEN)

                # Run OCR with optimized settings for sparse handwriting
                # PSM 11 = Sparse text - find as much text as possible
                # PSM 6 = Uniform block of text (alternative)
                custom_config = r"--psm 11 --oem 3"
                text = pytesseract.image_to_string(img, config=custom_config)

                if text.strip():
                    ocr_results.append(text.strip())

            except subprocess.TimeoutExpired:
                pass
            except FileNotFoundError:
                # rmc not installed
                return None
            finally:
                if tmp_svg_path:
                    tmp_svg_path.unlink(missing_ok=True)
                if tmp_png_path:
                    tmp_png_path.unlink(missing_ok=True)
                if tmp_raw_path:
                    tmp_raw_path.unlink(missing_ok=True)

        return ocr_results if ocr_results else None

    except ImportError:
        # OCR dependencies not installed
        return None
