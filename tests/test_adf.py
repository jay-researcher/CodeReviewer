from __future__ import annotations

import unittest

from code_reviewer.adf import ADFValidationError, adf_plain_text, render_adf_html, validate_adf


EXPAND_DOCUMENT = {
    "version": 1,
    "type": "doc",
    "content": [
        {
            "type": "expand",
            "attrs": {"title": "Evidence"},
            "content": [
                {
                    "type": "table",
                    "content": [
                        {
                            "type": "tableRow",
                            "content": [
                                {"type": "tableHeader", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Screenshot"}]}]},
                                {"type": "tableHeader", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Description"}]}]},
                            ],
                        },
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {
                                            "type": "mediaSingle",
                                            "attrs": {"layout": "center"},
                                            "content": [{"type": "media", "attrs": {"id": "shot-1", "type": "file", "alt": "Screenshot"}}],
                                        }
                                    ],
                                },
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {
                                            "type": "orderedList",
                                            "content": [
                                                {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "First"}]}]}
                                            ],
                                        },
                                        {
                                            "type": "bulletList",
                                            "content": [
                                                {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Second"}]}]}
                                            ],
                                        },
                                    ],
                                },
                            ],
                        },
                    ],
                }
            ],
        }
    ],
}


class ADFTests(unittest.TestCase):
    def test_expand_supports_table_lists_and_screenshot(self) -> None:
        self.assertIs(validate_adf(EXPAND_DOCUMENT), EXPAND_DOCUMENT)
        markup = render_adf_html(EXPAND_DOCUMENT, {"shot-1": "/api/draft-attachments/shot-1"})
        self.assertIn("<details", markup)
        self.assertIn("<table>", markup)
        self.assertIn("<ol", markup)
        self.assertIn("<ul>", markup)
        self.assertIn("/api/draft-attachments/shot-1", markup)
        self.assertIn("First", adf_plain_text(EXPAND_DOCUMENT))

    def test_expand_requires_title(self) -> None:
        document = {"version": 1, "type": "doc", "content": [{"type": "expand", "attrs": {}, "content": [{"type": "paragraph", "content": []}]}]}
        with self.assertRaises(ADFValidationError):
            validate_adf(document)

    def test_nested_expand_is_limited_to_table_cells(self) -> None:
        invalid = {"version": 1, "type": "doc", "content": [{"type": "nestedExpand", "attrs": {"title": "No"}, "content": [{"type": "paragraph", "content": []}]}]}
        with self.assertRaises(ADFValidationError):
            validate_adf(invalid)
        valid = {
            "version": 1,
            "type": "doc",
            "content": [{"type": "table", "content": [{"type": "tableRow", "content": [{"type": "tableCell", "content": [{"type": "nestedExpand", "attrs": {"title": "Details"}, "content": [{"type": "paragraph", "content": []}]}]}]}]}],
        }
        self.assertIs(validate_adf(valid), valid)

    def test_renderer_rejects_script_urls(self) -> None:
        document = {
            "version": 1,
            "type": "doc",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "unsafe", "marks": [{"type": "link", "attrs": {"href": "javascript:alert(1)"}}]}]}],
        }
        markup = render_adf_html(document)
        self.assertNotIn("javascript:", markup)
        self.assertIn('href="#"', markup)


if __name__ == "__main__":
    unittest.main()
