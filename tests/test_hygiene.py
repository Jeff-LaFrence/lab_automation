"""Running the tests writes nothing outside this repository."""

from __future__ import annotations

import os


def test_tmp_path_is_inside_the_repository(tmp_path, root):
    assert tmp_path.resolve().is_relative_to(root.resolve())


def test_library_config_folders_are_inside_the_repository(root):
    for var in ("OT_API_CONFIG_DIR", "MPLCONFIGDIR"):
        assert os.environ[var].startswith(str(root))
