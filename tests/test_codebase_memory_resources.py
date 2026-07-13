from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from code_reviewer.repository_sync import (
    _INDEX_THREAD_LOCK,
    _codebase_memory_index_lock,
    index_codebase_memory,
)


class CodebaseMemoryResourceTests(unittest.TestCase):
    def test_parallel_index_request_is_skipped(self) -> None:
        self.assertTrue(_INDEX_THREAD_LOCK.acquire(blocking=False))
        try:
            with _codebase_memory_index_lock() as acquired:
                self.assertFalse(acquired)
        finally:
            _INDEX_THREAD_LOCK.release()

    def test_oversized_repository_is_skipped_before_process_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch(
            "code_reviewer.repository_sync._tracked_file_count", return_value=50001
        ), patch("code_reviewer.repository_sync.run_utf8") as run:
            indexed, status = index_codebase_memory(Path(temp), "a" * 40, "large-project")

        self.assertFalse(indexed)
        self.assertIn("more than 50000 tracked files", status)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
