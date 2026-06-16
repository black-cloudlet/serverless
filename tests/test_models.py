import pytest
from pydantic import ValidationError

from app.models.common import EnvVar, FileMount, Scaling
from app.models.function import FunctionCreate


def test_valid_function():
    fn = FunctionCreate(
        name="my-fn", gitUrl="https://git/x.git", gitToken="t", runtime="python"
    )
    assert fn.branch == "main"
    assert fn.scaling.minScale == 0


def test_invalid_name_rejected():
    with pytest.raises(ValidationError):
        FunctionCreate(name="Bad_Name", gitUrl="g", gitToken="t", runtime="python")


def test_unsupported_runtime_rejected():
    with pytest.raises(ValidationError):
        FunctionCreate(name="x", gitUrl="g", gitToken="t", runtime="ruby")


def test_envvar_requires_exactly_one_source():
    with pytest.raises(ValidationError):
        EnvVar(name="X")
    EnvVar(name="X", value="1")


def test_filemount_inline_xor_source():
    FileMount(mountPath="/etc/a", content="hi")
    FileMount(mountPath="/etc/a", source="cm", type="configmap")
    with pytest.raises(ValidationError):
        FileMount(mountPath="/etc/a")
    with pytest.raises(ValidationError):
        FileMount(mountPath="/etc/a", content="x", source="cm")


def test_scaling_bounds():
    with pytest.raises(ValidationError):
        Scaling(minScale=5, maxScale=2)
