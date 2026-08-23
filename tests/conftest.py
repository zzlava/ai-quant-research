from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import CONFIG_DIR, PROJECT_ROOT


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def strategy_config_dir() -> Path:
    return CONFIG_DIR
