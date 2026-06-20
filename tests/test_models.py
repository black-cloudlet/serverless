import pytest
from pydantic import ValidationError

from app.models.common import EnvVar, FileMount, Scaling
from app.models.container import ContainerCreate
from app.models.function import FunctionCreate


def test_container_registry_creds_optional_but_paired():
    # both omitted -> public image, fine
    c = ContainerCreate(name="api", group="team", image="reg/api:1")
    assert c.registryUsername is None and c.registryToken is None
    # both provided -> fine
    ContainerCreate(name="api", group="team", image="reg/api:1",
                    registryUsername="bob", registryToken="t")
    # only one provided -> rejected
    with pytest.raises(ValidationError):
        ContainerCreate(name="api", group="team", image="reg/api:1", registryUsername="bob")
    with pytest.raises(ValidationError):
        ContainerCreate(name="api", group="team", image="reg/api:1", registryToken="t")


def test_valid_function():
    fn = FunctionCreate(
        name="my-fn", group="team", gitUrl="https://git/x.git", gitToken="t", runtime="python"
    )
    assert fn.branch == "main"
    assert fn.scaling.minScale == 0


def test_invalid_name_rejected():
    with pytest.raises(ValidationError):
        FunctionCreate(name="Bad_Name", group="team", gitUrl="g", gitToken="t", runtime="python")


def test_unsupported_runtime_rejected():
    with pytest.raises(ValidationError):
        FunctionCreate(name="x", group="team", gitUrl="g", gitToken="t", runtime="ruby")


def test_size_default_and_choices():
    fn = FunctionCreate(name="x", group="team", gitUrl="g", gitToken="t", runtime="go")
    assert fn.size == "small"  # default
    assert (
        FunctionCreate(
            name="x", group="team", gitUrl="g", gitToken="t", runtime="go", size="large"
        ).size
        == "large"
    )
    with pytest.raises(ValidationError):  # unknown size
        FunctionCreate(
            name="x", group="team", gitUrl="g", gitToken="t", runtime="go", size="xl"
        )


def test_envvar_requires_value():
    with pytest.raises(ValidationError):
        EnvVar(name="X")
    e = EnvVar(name="X", value="1")
    assert e.secret is False
    assert EnvVar(name="X", value="1", secret=True).secret is True


def test_filemount_requires_inline_content():
    f = FileMount(mountPath="/etc/a", content="hi")
    assert f.readOnly is True
    assert FileMount(mountPath="/etc/a", content="hi", readOnly=False).readOnly is False
    with pytest.raises(ValidationError):
        FileMount(mountPath="/etc/a")  # no content
    with pytest.raises(ValidationError):
        FileMount(mountPath="/etc/a", content="x", contentBase64="eA==")


def test_scaling_bounds():
    with pytest.raises(ValidationError):
        Scaling(minScale=5, maxScale=2)


def test_scaling_metric_default_and_choices():
    assert Scaling().metric == "concurrency"  # KPA default
    assert Scaling().autoscaler_class is None
    assert Scaling(metric="rps", target=20).autoscaler_class is None
    for hpa_metric in ("cpu", "memory"):
        assert Scaling(metric=hpa_metric, minScale=1, target=70).autoscaler_class == (
            "hpa.autoscaling.knative.dev"
        )
    with pytest.raises(ValidationError):  # unknown metric
        Scaling(metric="bananas")


def test_scaling_effective_target_is_metric_aware():
    # KPA metrics default to 100; cpu/memory default to 70% so we scale early.
    assert Scaling().effective_target == 100
    assert Scaling(metric="rps").effective_target == 100
    assert Scaling(metric="cpu", minScale=1).effective_target == 70
    assert Scaling(metric="memory", minScale=1).effective_target == 70
    # explicit target always wins
    assert Scaling(metric="cpu", minScale=1, target=55).effective_target == 55
    assert Scaling(target=200).effective_target == 200  # concurrency: no % cap


def test_scaling_cpu_memory_target_is_a_percentage():
    # utilization percentage targets above 100 make no sense.
    for hpa_metric in ("cpu", "memory"):
        with pytest.raises(ValidationError):
            Scaling(metric=hpa_metric, minScale=1, target=150)
        Scaling(metric=hpa_metric, minScale=1, target=100)  # ok


def test_scaling_hpa_metric_cannot_scale_to_zero():
    # cpu/memory use HPA, which can't scale to zero; minScale must be >= 1.
    for hpa_metric in ("cpu", "memory"):
        with pytest.raises(ValidationError):
            Scaling(metric=hpa_metric, minScale=0)
        Scaling(metric=hpa_metric, minScale=1)  # ok


def test_optional_hostname_validated():
    fn = FunctionCreate(
        name="my-fn",
        group="team", gitUrl="g",
        gitToken="t",
        runtime="python",
        hostname="app.example.com",
    )
    assert fn.hostname == "app.example.com"
    # default (no hostname) is allowed
    assert FunctionCreate(name="x", group="team", gitUrl="g", gitToken="t", runtime="go").hostname is None
    # invalid hostnames rejected
    for bad in ["NoDots", "UPPER.example.com", "bad_host.example.com"]:
        with pytest.raises(ValidationError):
            FunctionCreate(
                name="x", group="team", gitUrl="g", gitToken="t", runtime="go", hostname=bad
            )
