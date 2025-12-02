"""
Sampling-based OCR for reMarkable documents.

This module provides OCR functionality using MCP's sampling capability,
allowing the host application's LLM to extract text from images.

## Usage

Sampling OCR is only available when:
1. The client supports the sampling capability
2. REMARKABLE_OCR_BACKEND is set to "sampling" or "auto" (and no Google Vision key is set)

The key advantage of sampling-based OCR is that it uses the client's own model,
which may provide better results for handwriting without requiring additional
API keys or services.

## Important Notes

- Sampling is asynchronous and requires a Context object from tool execution
- The prompt is carefully crafted to return ONLY the extracted text
- Falls back to tesseract if sampling is not available or fails
"""

import base64
from typing import TYPE_CHECKING, List, Optional

from mcp.types import ImageContent, SamplingMessage, TextContent

if TYPE_CHECKING:
    from mcp.server.fastmcp import Context
    from mcp.types import CreateMessageResult


# The OCR prompt is carefully designed to extract ONLY the text content
# with no additional commentary, explanations, or formatting.
OCR_SYSTEM_PROMPT = """You are an OCR system. Extract the exact text visible in the image.

CRITICAL RULES:
1. Output ONLY the text found in the image, nothing else
2. Do NOT add any commentary, explanations, or descriptions
3. Do NOT use phrases like "The text says:" or "I can see:"
4. Do NOT describe the image or its contents
5. Preserve the original text layout and line breaks where possible
6. If no text is visible, output exactly: [NO TEXT DETECTED]
7. If text is unclear, transcribe what you can and use [...] for unclear portions

You are extracting handwritten notes from a reMarkable tablet. Focus on accuracy."""

OCR_USER_PROMPT = "Extract all text from this image. Output only the text content, nothing else."


async def ocr_via_sampling(
    ctx: "Context",
    png_data: bytes,
    max_tokens: int = 2000,
) -> Optional[str]:
    """
    Perform OCR on an image using the client's LLM via MCP sampling.

    Args:
        ctx: The FastMCP Context object from a tool function
        png_data: PNG image bytes to perform OCR on
        max_tokens: Maximum tokens for the response (default: 2000)

    Returns:
        Extracted text from the image, or None if OCR failed

    Example:
        @mcp.tool()
        async def my_ocr_tool(document: str, ctx: Context) -> str:
            # ... get png_data from document ...
            text = await ocr_via_sampling(ctx, png_data)
            if text:
                return text
            return "OCR failed"
    """
    try:
        session = ctx.session
        if not session:
            return None

        # Encode image as base64
        image_b64 = base64.b64encode(png_data).decode("utf-8")

        # Create the sampling message with image
        messages = [
            SamplingMessage(
                role="user",
                content=ImageContent(
                    type="image",
                    data=image_b64,
                    mimeType="image/png",
                ),
            ),
            SamplingMessage(
                role="user",
                content=TextContent(type="text", text=OCR_USER_PROMPT),
            ),
        ]

        # Request completion from the client's LLM
        result: "CreateMessageResult" = await session.create_message(
            messages=messages,
            system_prompt=OCR_SYSTEM_PROMPT,
            max_tokens=max_tokens,
            temperature=0.0,  # Use low temperature for consistency
        )

        # Extract text from the result
        if result and result.content:
            if isinstance(result.content, TextContent):
                text = result.content.text
            elif hasattr(result.content, "text"):
                text = result.content.text
            else:
                return None

            # Check for "no text" response
            if text and "[NO TEXT DETECTED]" not in text:
                return text.strip()

        return None

    except Exception:
        # Sampling failed, caller should fall back to other OCR methods
        return None


async def ocr_pages_via_sampling(
    ctx: "Context",
    png_data_list: List[bytes],
    max_tokens: int = 2000,
) -> Optional[List[str]]:
    """
    Perform OCR on multiple pages using the client's LLM via MCP sampling.

    Args:
        ctx: The FastMCP Context object from a tool function
        png_data_list: List of PNG image bytes to perform OCR on
        max_tokens: Maximum tokens for each response (default: 2000)

    Returns:
        List of extracted text (one per page), or None if all pages failed
    """
    results = []
    has_any_result = False

    for png_data in png_data_list:
        text = await ocr_via_sampling(ctx, png_data, max_tokens)
        if text:
            results.append(text)
            has_any_result = True
        else:
            results.append("")  # Empty string for failed pages

    return results if has_any_result else None


def get_ocr_backend() -> str:
    """
    Get the configured OCR backend.

    Returns one of: "sampling", "google", "tesseract", "auto"

    The "auto" mode selects the best available backend:
    1. Google Vision if GOOGLE_VISION_API_KEY is set
    2. Tesseract otherwise

    To use sampling OCR, explicitly set REMARKABLE_OCR_BACKEND=sampling.
    Sampling OCR requires a client that supports the sampling capability.
    """
    import os

    return os.environ.get("REMARKABLE_OCR_BACKEND", "auto").lower()


def should_use_sampling_ocr(ctx: "Context") -> bool:
    """
    Check if sampling-based OCR should be used.

    Returns True if:
    1. REMARKABLE_OCR_BACKEND is explicitly set to "sampling", AND
    2. The client supports the sampling capability

    Args:
        ctx: The FastMCP Context object

    Returns:
        True if sampling OCR should be used, False otherwise
    """
    from remarkable_mcp.capabilities import client_supports_sampling

    backend = get_ocr_backend()

    # Only use sampling if explicitly configured
    if backend != "sampling":
        return False

    # Check if client supports sampling
    return client_supports_sampling(ctx)
