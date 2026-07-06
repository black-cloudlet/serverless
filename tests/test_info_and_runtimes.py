from app.models.common import Scaling
from app.services.ksvc import workload_sizes
from app.services.runtimes import RuntimeRegistry, RuntimeSpec, load_runtimes


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


def test_load_runtimes_missing_file_falls_back(tmp_path):
    reg = load_runtimes(str(tmp_path / "does-not-exist.yaml"))
    assert reg.names() == ["python", "go", "javascript"]


def test_load_runtimes_empty_list_falls_back(tmp_path):
    f = tmp_path / "runtimes.yaml"
    f.write_text("runtimes: []\n")
    assert load_runtimes(str(f)).names() == ["python", "go", "javascript"]


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
