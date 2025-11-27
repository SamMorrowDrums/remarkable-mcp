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
                    output_width=1404,
                    output_height=1872,
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
                        output_width=1404,  # reMarkable width
                        output_height=1872,  # reMarkable height
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
