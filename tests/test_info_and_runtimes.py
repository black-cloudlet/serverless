from api.models.common import Scaling
from api.services.builder.runtimes import RuntimeRegistry, RuntimeSpec, load_runtimes
from api.services.manifests.ksvc import workload_sizes


def test_load_runtimes_from_file(tmp_path):
    f = tmp_path / "runtimes.yaml"
    f.write_text(
        "runtimes:\n"
        "  - name: python\n"
        "    versions: ['3.13']\n"  # extra fields are preserved (forward-compat)
        "  - name: go\n"
    )
    reg = load_runtimes(str(f))
    assert reg.names() == ["python", "go"]
    assert reg.has("python") and not reg.has("ruby")
    # unknown keys are kept on the spec for future use
    assert reg.specs[0].model_dump()["versions"] == ["3.13"]


def test_a_missing_runtimes_file_is_fatal(tmp_path):
    """No fallback: a default list would name no Builder, so the API would
    accept functions it can never build and only fail minutes later."""
    import pytest

    from api.services.builder.runtimes import RuntimeConfigError

    with pytest.raises(RuntimeConfigError, match="not found"):
        load_runtimes(str(tmp_path / "does-not-exist.yaml"))


def test_an_empty_runtimes_file_is_fatal(tmp_path):
    import pytest

    from api.services.builder.runtimes import RuntimeConfigError

    f = tmp_path / "runtimes.yaml"
    f.write_text("runtimes: []\n")
    with pytest.raises(RuntimeConfigError, match="declares no runtimes"):
        load_runtimes(str(f))


def test_a_malformed_runtimes_file_is_fatal(tmp_path):
    import pytest

    from api.services.builder.runtimes import RuntimeConfigError

    f = tmp_path / "runtimes.yaml"
    f.write_text("runtimes:\n  - versions: ['3.13']\n")  # no `name`
    with pytest.raises(RuntimeConfigError, match="malformed"):
        load_runtimes(str(f))


def test_startup_fails_when_the_runtimes_file_is_missing(monkeypatch, tmp_path):
    """The lifespan loads it, so a broken mount never reaches readiness."""
    import pytest

    from api.core.config import get_settings
    from api.dependencies import get_runtimes
    from api.services.builder.runtimes import RuntimeConfigError

    monkeypatch.setenv("SERVERLESS_RUNTIMES_FILE", str(tmp_path / "absent.yaml"))
    get_settings.cache_clear()
    get_runtimes.cache_clear()
    with pytest.raises(RuntimeConfigError):
        get_runtimes()


def test_registry_helpers():
    reg = RuntimeRegistry([RuntimeSpec(name="python"), RuntimeSpec(name="go")])
    assert reg.has("go") and not reg.has("node")
    assert reg.names() == ["python", "go"]


def test_workload_sizes():
    assert workload_sizes() == ["small", "medium", "large"]


def test_scaling_capabilities_match_the_model():
    caps = Scaling.capabilities()
    assert caps.defaultMetric == "concurrency"
    assert caps.scaleDownDelay.max == "1h" and caps.scaleDownDelay.min == "0s"
    assert caps.scaleDownDelay.default is None

    by_name = {m.name: m for m in caps.metrics}
    assert set(by_name) == {"concurrency", "rps", "cpu", "memory"}

    # KPA metrics: scale-to-zero, absolute target, no upper bound.
    for name in ("concurrency", "rps"):
        m = by_name[name]
        assert m.minScaleFloor == 0
        assert m.target.default == 100 and m.target.max is None and m.target.min == 1
    assert by_name["concurrency"].target.unit == "concurrentRequests"
    assert by_name["rps"].target.unit == "requestsPerSecond"

    # HPA metrics: no scale-to-zero, percent target capped at 100.
    for name in ("cpu", "memory"):
        m = by_name[name]
        assert m.minScaleFloor == 1
        assert m.target.default == 70 and m.target.max == 100 and m.target.unit == "percent"


def test_capabilities_track_a_default_change():
    # The projection is derived, not a second copy: flipping the model's default
    # metric moves the advertised default with it.
    original = Scaling.model_fields["metric"].default
    Scaling.model_fields["metric"].default = "cpu"
    try:
        assert Scaling.capabilities().defaultMetric == "cpu"
    finally:
        Scaling.model_fields["metric"].default = original


def _fn_service(**over):
    """A FunctionService over a registry whose go runtime offers three versions."""
    from api.services.builder.runtimes import RuntimeRegistry, RuntimeSpec
    from api.services.function import FunctionService

    kwargs = dict(
        name="go",
        builder="go",
        versionEnv="BP_GO_VERSION",
        defaultVersion="1.24",
        versions=["1.23", "1.24", "1.25"],
    )
    kwargs.update(over)
    return FunctionService(object(), RuntimeRegistry([RuntimeSpec(**kwargs)]))


def test_a_version_must_be_one_the_runtime_advertises():
    """The list /info offers and the list a create accepts are the same list.

    Airgapped this is not a formality: only the advertised versions are
    mirrored, so an unlisted one has no toolchain to download and would fail
    deep inside the build instead of at the edge.
    """
    import pytest

    from common.errors import ValidationError

    svc = _fn_service()
    svc._assert_runtime("go", "1.25")  # advertised -> accepted
    svc._assert_runtime("go", None)  # omitted -> the default

    with pytest.raises(ValidationError, match="unsupported version '1.19'"):
        svc._assert_runtime("go", "1.19")


def test_the_rejection_names_the_versions_that_would_work():
    import pytest

    from common.errors import ValidationError

    with pytest.raises(ValidationError, match="available versions: 1.23, 1.24, 1.25"):
        _fn_service()._assert_runtime("go", "1.19")


def test_a_runtime_that_offers_no_choice_rejects_a_version():
    """Empty `versions` means the runtime pins its own (see RuntimeCapability).

    Accepting a version there would silently ignore it, which is the failure
    mode this whole change exists to remove.
    """
    import pytest

    from common.errors import ValidationError

    for pins in (_fn_service(versions=[]), _fn_service(versionEnv=None)):
        with pytest.raises(ValidationError, match="does not offer a choice of version"):
            pins._assert_runtime("go", "1.25")
        pins._assert_runtime("go", None)  # omitting is always fine


def test_info_advertises_the_versions_a_create_will_now_accept():
    """The list was published long before anything consumed it."""
    from fastapi.testclient import TestClient

    from api.main import create_app

    body = TestClient(create_app()).get("/api/v1/functions/info").json()
    go = next(r for r in body["runtimes"] if r["name"] == "go")
    assert go["versions"] and go["defaultVersion"] in go["versions"]
