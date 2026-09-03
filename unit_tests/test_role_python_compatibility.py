import ast
from pathlib import Path

import pytest

ROLE_PYTHON_FILES = sorted(Path("roles").glob("*/files/**/*.py"))


@pytest.mark.parametrize("path", ROLE_PYTHON_FILES, ids=str)
def test_role_python_files_support_noble_python(path: Path) -> None:
    ast.parse(path.read_text(), filename=str(path), feature_version=(3, 12))
