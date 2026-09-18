from typing import Any


BLOCK_TYPES = {
    "blockquote",
    "bulletList",
    "codeBlock",
    "heading",
    "listItem",
    "orderedList",
    "paragraph",
    "table",
    "tableCell",
    "tableHeader",
    "tableRow",
}


def adf_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(adf_to_text(item) for item in value).strip()
    if not isinstance(value, dict):
        return str(value)

    node_type = value.get("type")
    if node_type == "text":
        return str(value.get("text", ""))
    if node_type == "hardBreak":
        return "\n"

    text = "".join(adf_to_text(item) for item in value.get("content", []))
    if node_type in BLOCK_TYPES and text and not text.endswith("\n"):
        text += "\n"
    return text.strip() if node_type == "doc" else text


def text_to_adf(text: str) -> dict[str, Any]:
    paragraphs = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        paragraph: dict[str, Any] = {"type": "paragraph"}
        if line:
            paragraph["content"] = [{"type": "text", "text": line}]
        paragraphs.append(paragraph)

    return {
        "version": 1,
        "type": "doc",
        "content": paragraphs or [{"type": "paragraph"}],
    }
