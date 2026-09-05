"""Every key in `.env.example` must name a setting the code reads.

Settings ignore unknown environment variables (``extra="ignore"``), so a key
that names nothing fails nowhere else. These resolve each key to the field it
fills, then load the file the way a process does.
"""

from __future__ import annotations

import pathlib

import pytest
from pydantic import BaseModel

from api.core.config import Settings
from build_controller.config import BuildControllerSettings
from tenant_controller.config import TenantControllerSettings

ENV_EXAMPLE = pathlib.Path(__file__).resolve().parent.parent / ".env.example"
PREFIX = "SERVERLESS_"
DELIMITER = "__"

# The file documents the API and, in its last section, the two controllers.
MODELS = (Settings, BuildControllerSettings, TenantControllerSettings)


def _keys():
    """Every SERVERLESS_* key the file sets, in file order."""
    for line in ENV_EXAMPLE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name = line.split("=", 1)[0]
        if name.startswith(PREFIX):
            yield name


def _resolves(model: type[BaseModel], path: list[str]) -> bool:
    """Whether ``path`` names a field of ``model``, descending nested models."""
    head, rest = path[0], path[1:]
    field = model.model_fields.get(head)
    if field is None:
        return False
    if not rest:
        return True
    annotation = field.annotation
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _resolves(annotation, rest)
    return False  # nested key under a field that is not a model


@pytest.mark.parametrize("key", list(_keys()))
def test_every_documented_variable_is_one_the_code_reads(key):
    path = key[len(PREFIX) :].lower().split(DELIMITER)
    assert any(_resolves(model, path) for model in MODELS), (
        f"{key} in .env.example names no setting; it was renamed or removed"
    )


def test_the_file_loads_into_settings():
    """The values, not just the names."""
    settings = Settings(_env_file=str(ENV_EXAMPLE))

    assert [region.name for region in settings.regions] == ["central", "south"]
    assert settings.local_region == "central"
