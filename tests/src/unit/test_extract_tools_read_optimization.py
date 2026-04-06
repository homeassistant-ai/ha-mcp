"""Tests for redundant file-read elimination in update_readme / update_docs.

Verifies that when content= is provided, the functions use the supplied
string and do NOT perform an additional read from disk — closing the
double-read pattern in check_sync() identified in issue #885.

Two test dimensions per function (bidirectional):
  - Optimisation: file is NOT read when content= is provided
  - Correctness: result is identical to the internal-read path
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import extract_tools  # noqa: E402


class TestUpdateReadmeContentParam:
    """update_readme(tools, content=...) avoids redundant disk reads."""

    def test_readme_file_not_read_when_content_provided(self):
        """Optimisation: README_PATH.read_text must not be called when content= given."""
        tools = extract_tools.extract_tools()
        readme = extract_tools.README_PATH.read_text()

        mock_path = MagicMock(spec=Path)
        mock_path.read_text.return_value = "should not be used"

        with patch.object(extract_tools, "README_PATH", mock_path):
            result = extract_tools.update_readme(tools, content=readme)

        mock_path.read_text.assert_not_called()
        assert result is not None

    def test_readme_content_param_produces_same_result_as_file_read(self):
        """Correctness: result via content= matches result via internal file read."""
        tools = extract_tools.extract_tools()
        readme = extract_tools.README_PATH.read_text()

        result_via_param = extract_tools.update_readme(tools, content=readme)
        result_via_file = extract_tools.update_readme(tools)

        assert result_via_param == result_via_file


class TestUpdateDocsContentParam:
    """update_docs(tools, content=...) avoids redundant disk reads."""

    def test_docs_file_not_read_when_content_provided(self):
        """Optimisation: DOCS_PATH.read_text must not be called when content= given."""
        if not extract_tools.DOCS_PATH.exists():
            import pytest

            pytest.skip("DOCS_PATH not found — skipping")

        tools = extract_tools.extract_tools()
        docs = extract_tools.DOCS_PATH.read_text(encoding="utf-8")

        mock_path = MagicMock(spec=Path)
        mock_path.read_text.return_value = "should not be used"

        with patch.object(extract_tools, "DOCS_PATH", mock_path):
            result = extract_tools.update_docs(tools, content=docs)

        mock_path.read_text.assert_not_called()
        assert result is not None

    def test_docs_content_param_produces_same_result_as_file_read(self):
        """Correctness: result via content= matches result via internal file read."""
        if not extract_tools.DOCS_PATH.exists():
            import pytest

            pytest.skip("DOCS_PATH not found — skipping")

        tools = extract_tools.extract_tools()
        docs = extract_tools.DOCS_PATH.read_text(encoding="utf-8")

        result_via_param = extract_tools.update_docs(tools, content=docs)
        result_via_file = extract_tools.update_docs(tools)

        assert result_via_param == result_via_file
