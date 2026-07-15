from __future__ import annotations

import html
import json
from typing import Any
from urllib.parse import urlparse


ADF_VERSION = 1
ADF_BLOCK_NODES = {
    "blockquote",
    "bulletList",
    "codeBlock",
    "expand",
    "heading",
    "mediaGroup",
    "mediaSingle",
    "nestedExpand",
    "orderedList",
    "panel",
    "paragraph",
    "rule",
    "table",
}
ADF_INLINE_NODES = {"date", "emoji", "hardBreak", "inlineCard", "mention", "status", "text"}
ADF_CONTAINER_NODES = {
    "blockquote",
    "bulletList",
    "doc",
    "expand",
    "listItem",
    "mediaGroup",
    "mediaSingle",
    "nestedExpand",
    "orderedList",
    "panel",
    "table",
    "tableCell",
    "tableHeader",
    "tableRow",
}
ADF_ALLOWED_NODES = ADF_BLOCK_NODES | ADF_INLINE_NODES | ADF_CONTAINER_NODES | {"media"}
ADF_ALLOWED_MARKS = {"backgroundColor", "code", "em", "link", "strike", "strong", "subsup", "textColor", "underline"}


class ADFValidationError(ValueError):
    pass


def _safe_url(value: str, *, image: bool = False) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    allowed = {"http", "https"} if image else {"http", "https", "mailto"}
    if parsed.scheme and parsed.scheme.lower() not in allowed:
        return ""
    if not parsed.scheme and not text.startswith(("/", "./", "../", "#")):
        return ""
    return text


def empty_adf() -> dict[str, Any]:
    return {"version": ADF_VERSION, "type": "doc", "content": [{"type": "paragraph", "content": []}]}


def text_adf(value: str) -> dict[str, Any]:
    paragraphs = []
    for line in (value or "").splitlines() or [""]:
        paragraphs.append({"type": "paragraph", "content": ([{"type": "text", "text": line}] if line else [])})
    return {"version": ADF_VERSION, "type": "doc", "content": paragraphs}


def validate_adf(document: object, *, max_depth: int = 12, max_nodes: int = 5000) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ADFValidationError("ADF document must be an object.")
    if document.get("version") != ADF_VERSION or document.get("type") != "doc":
        raise ADFValidationError("ADF document must have version 1 and type doc.")
    counter = [0]
    _validate_node(document, depth=0, max_depth=max_depth, max_nodes=max_nodes, counter=counter, parent="")
    return document


def _validate_node(
    node: object,
    *,
    depth: int,
    max_depth: int,
    max_nodes: int,
    counter: list[int],
    parent: str,
) -> None:
    if not isinstance(node, dict):
        raise ADFValidationError("Every ADF node must be an object.")
    counter[0] += 1
    if counter[0] > max_nodes:
        raise ADFValidationError(f"ADF document exceeds {max_nodes} nodes.")
    if depth > max_depth:
        raise ADFValidationError(f"ADF nesting exceeds {max_depth} levels.")
    node_type = str(node.get("type") or "")
    if node_type not in ADF_ALLOWED_NODES:
        raise ADFValidationError(f"Unsupported ADF node: {node_type or '<empty>'}.")
    if node_type == "text" and not isinstance(node.get("text"), str):
        raise ADFValidationError("ADF text nodes require a text value.")
    marks = node.get("marks", [])
    if marks is not None:
        if not isinstance(marks, list):
            raise ADFValidationError("ADF marks must be a list.")
        for mark in marks:
            if not isinstance(mark, dict) or str(mark.get("type") or "") not in ADF_ALLOWED_MARKS:
                raise ADFValidationError("ADF contains an unsupported mark.")
    attrs = node.get("attrs")
    if attrs is not None and not isinstance(attrs, dict):
        raise ADFValidationError("ADF attrs must be an object.")
    if node_type in {"expand", "nestedExpand"}:
        title = str((attrs or {}).get("title") or "").strip()
        if not title:
            raise ADFValidationError(f"ADF {node_type} requires attrs.title.")
        if node_type == "expand" and parent != "doc":
            raise ADFValidationError("ADF expand is a top-level node.")
        if node_type == "nestedExpand" and parent not in {"tableCell", "tableHeader"}:
            raise ADFValidationError("ADF nestedExpand is only valid inside tableCell or tableHeader.")
    if node_type == "media":
        media_attrs = attrs or {}
        if not str(media_attrs.get("id") or "").strip():
            raise ADFValidationError("ADF media requires attrs.id.")
        if str(media_attrs.get("type") or "") not in {"file", "link"}:
            raise ADFValidationError("ADF media attrs.type must be file or link.")
        if parent not in {"mediaGroup", "mediaSingle"}:
            raise ADFValidationError("ADF media must be inside mediaGroup or mediaSingle.")
    content = node.get("content", [])
    if content is None:
        content = []
    if not isinstance(content, list):
        raise ADFValidationError("ADF content must be a list.")
    if node_type in ADF_CONTAINER_NODES and node_type != "doc" and not content:
        raise ADFValidationError(f"ADF {node_type} requires content.")
    for child in content:
        _validate_node(
            child,
            depth=depth + 1,
            max_depth=max_depth,
            max_nodes=max_nodes,
            counter=counter,
            parent=node_type,
        )


def adf_plain_text(document: object) -> str:
    validate_adf(document)
    lines: list[str] = []

    def visit(node: dict[str, Any]) -> None:
        if node.get("type") == "text":
            lines.append(str(node.get("text") or ""))
        elif node.get("type") in {"paragraph", "heading", "listItem", "tableRow"} and lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        for child in node.get("content") or []:
            visit(child)

    visit(document)  # type: ignore[arg-type]
    return "".join(lines).strip()


def render_adf_html(document: object, media_urls: dict[str, str] | None = None) -> str:
    validate_adf(document)
    media_urls = media_urls or {}

    def render(node: dict[str, Any]) -> str:
        node_type = str(node.get("type") or "")
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        children = "".join(render(child) for child in node.get("content") or [])
        if node_type == "doc":
            return f'<div class="adf-document">{children}</div>'
        if node_type == "text":
            value = html.escape(str(node.get("text") or ""))
            for mark in node.get("marks") or []:
                mark_type = mark.get("type")
                mark_attrs = mark.get("attrs") if isinstance(mark.get("attrs"), dict) else {}
                if mark_type == "strong":
                    value = f"<strong>{value}</strong>"
                elif mark_type == "em":
                    value = f"<em>{value}</em>"
                elif mark_type == "code":
                    value = f"<code>{value}</code>"
                elif mark_type == "link":
                    href = html.escape(_safe_url(str(mark_attrs.get("href") or "")) or "#", quote=True)
                    value = f'<a href="{href}" target="_blank" rel="noopener noreferrer">{value}</a>'
            return value
        if node_type == "paragraph":
            return f"<p>{children or '<br>'}</p>"
        if node_type == "heading":
            level = max(1, min(6, int(attrs.get("level") or 2)))
            return f"<h{level}>{children}</h{level}>"
        if node_type == "bulletList":
            return f"<ul>{children}</ul>"
        if node_type == "orderedList":
            start = max(1, int(attrs.get("order") or 1))
            return f'<ol start="{start}">{children}</ol>'
        if node_type == "listItem":
            return f"<li>{children}</li>"
        if node_type == "table":
            return f'<div class="adf-table-wrap"><table>{children}</table></div>'
        if node_type == "tableRow":
            return f"<tr>{children}</tr>"
        if node_type == "tableHeader":
            return f"<th>{children}</th>"
        if node_type == "tableCell":
            return f"<td>{children}</td>"
        if node_type in {"expand", "nestedExpand"}:
            title = html.escape(str(attrs.get("title") or "Details"))
            return f'<details class="adf-expand"><summary>{title}</summary><div>{children}</div></details>'
        if node_type == "panel":
            panel_type = html.escape(str(attrs.get("panelType") or "info"), quote=True)
            return f'<aside class="adf-panel" data-panel-type="{panel_type}">{children}</aside>'
        if node_type == "blockquote":
            return f"<blockquote>{children}</blockquote>"
        if node_type == "codeBlock":
            language = html.escape(str(attrs.get("language") or ""), quote=True)
            return f'<pre><code data-language="{language}">{children}</code></pre>'
        if node_type == "rule":
            return "<hr>"
        if node_type == "hardBreak":
            return "<br>"
        if node_type in {"mediaGroup", "mediaSingle"}:
            return f'<div class="adf-media-group">{children}</div>'
        if node_type == "media":
            media_id = str(attrs.get("id") or "")
            source = _safe_url(media_urls.get(media_id) or str(attrs.get("url") or ""), image=True)
            alt = html.escape(str(attrs.get("alt") or attrs.get("name") or "Attachment"), quote=True)
            if source:
                return f'<img class="adf-media" src="{html.escape(source, quote=True)}" alt="{alt}" loading="lazy">'
            return f'<span class="adf-media-missing">{alt}</span>'
        if node_type == "mention":
            return f'<span class="adf-mention">@{html.escape(str(attrs.get("text") or attrs.get("id") or "user"))}</span>'
        if node_type == "emoji":
            return html.escape(str(attrs.get("text") or attrs.get("shortName") or ""))
        return children

    return render(document)  # type: ignore[arg-type]


def adf_json(document: object) -> str:
    return json.dumps(validate_adf(document), ensure_ascii=False, separators=(",", ":"))
