"""
MCP Prompts for reMarkable tablet workflows.

Pre-built prompts to help users get started with common tasks.
"""

from remarkable_mcp.server import mcp


@mcp.prompt(
    name="summarize_recent",
    title="Summarize Recent Notes",
    description="Get an AI summary of your recent reMarkable notes",
)
def summarize_recent_prompt() -> list:
    """Prompt to summarize recent documents."""
    return [
        {
            "role": "user",
            "content": (
                "Please check my recent reMarkable documents using remarkable_recent() "
                "and provide a summary of what I've been working on. "
                "For any documents that look interesting, read their content "
                "with remarkable_read() and give me key highlights."
            ),
        }
    ]


@mcp.prompt(
    name="find_notes",
    title="Find Notes About a Topic",
    description="Search your reMarkable tablet for notes on a specific topic",
)
def find_notes_prompt(topic: str) -> list:
    """Prompt to find notes about a topic."""
    return [
        {
            "role": "user",
            "content": (
                f"Search my reMarkable tablet for any notes about '{topic}'. "
                f"Use remarkable_browse(query='{topic}') to find relevant documents, "
                "then use remarkable_read() to extract and summarize the content. "
                "Please organize the information you find."
            ),
        }
    ]


@mcp.prompt(
    name="daily_review",
    title="Daily Notes Review",
    description="Review what you worked on today in your reMarkable tablet",
)
def daily_review_prompt() -> list:
    """Prompt for daily review of notes."""
    return [
        {
            "role": "user",
            "content": (
                "Please do a daily review of my reMarkable notes:\n\n"
                "1. Use remarkable_recent(limit=5, include_preview=True) to see "
                "what I worked on recently\n"
                "2. For documents modified today, read the full content\n"
                "3. Summarize the key points and any action items\n"
                "4. Suggest any follow-up tasks based on my notes"
            ),
        }
    ]


@mcp.prompt(
    name="export_document",
    title="Export Document Content",
    description="Extract and format content from a specific document",
)
def export_document_prompt(document_name: str) -> list:
    """Prompt to export a specific document."""
    return [
        {
            "role": "user",
            "content": (
                f"Please extract all the content from my reMarkable document "
                f"'{document_name}' using remarkable_read('{document_name}'). "
                "Then format it nicely as markdown, preserving the structure "
                "and any important formatting."
            ),
        }
    ]


@mcp.prompt(
    name="organize_library",
    title="Organize My Library",
    description="Get suggestions for organizing your reMarkable library",
)
def organize_library_prompt() -> list:
    """Prompt for library organization suggestions."""
    return [
        {
            "role": "user",
            "content": (
                "Please help me organize my reMarkable library:\n\n"
                "1. Use remarkable_browse('/') to see my current folder structure\n"
                "2. Use remarkable_recent(limit=20) to see my active documents\n"
                "3. Identify any documents that might be misplaced or could be "
                "better organized\n"
                "4. Suggest a folder structure that would help me stay organized\n\n"
                "Note: This server is read-only, so just provide recommendations - "
                "I'll reorganize manually on my tablet."
            ),
        }
    ]


@mcp.prompt(
    name="meeting_notes",
    title="Extract Meeting Notes",
    description="Find and extract meeting notes from your reMarkable",
)
def meeting_notes_prompt(meeting_keyword: str = "meeting") -> list:
    """Prompt to find and extract meeting notes."""
    return [
        {
            "role": "user",
            "content": (
                f"Find all my meeting notes on my reMarkable tablet:\n\n"
                f"1. Search for documents with remarkable_browse(query='{meeting_keyword}')\n"
                "2. Read the content of each meeting notes document\n"
                "3. Extract key decisions, action items, and attendees mentioned\n"
                "4. Create a consolidated summary of all meetings"
            ),
        }
    ]
