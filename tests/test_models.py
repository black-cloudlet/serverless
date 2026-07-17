import pytest
from pydantic import ValidationError

from api.models.common import EnvVar, FileMount, Scaling
from api.models.container import ContainerCreate, ContainerUpdate
from api.models.function import FunctionCreate, FunctionUpdate


def test_container_update_creds_keep_or_rotate():
    ContainerUpdate()  # no creds -> keep existing, fine
    ContainerUpdate(registryUsername="bob", registryToken="t")  # rotate
    # username alone (echoing the redacted read) -> keep existing token, fine
    ContainerUpdate(registryUsername="bob")
    # a token with no username is meaningless -> rejected
    with pytest.raises(ValidationError):
        ContainerUpdate(registryToken="t")


def test_function_update_build_inputs_do_not_require_token():
    # The token is stored, so changing a build input no longer requires the client
    # to re-send it - the service reuses the stored token. The rebuild decision
    # lives in the service, not the model.
    FunctionUpdate(scaling=Scaling(minScale=1, maxScale=1))  # config-only, fine
    FunctionUpdate(branch="release")  # build input without a token: accepted
    FunctionUpdate(runtime="go")
    FunctionUpdate(gitRepo="https://git/x.git", runtime="go", gitToken="t")


def test_container_registry_creds_optional_but_paired():
    # both omitted -> public image, fine
    c = ContainerCreate(name="api", image="reg/api:1")
    assert c.registryUsername is None and c.registryToken is None
    # both provided -> fine
    ContainerCreate(name="api", image="reg/api:1", registryUsername="bob", registryToken="t")
    # only one provided -> rejected
    with pytest.raises(ValidationError):
        ContainerCreate(name="api", image="reg/api:1", registryUsername="bob")
    with pytest.raises(ValidationError):
        ContainerCreate(name="api", image="reg/api:1", registryToken="t")


def test_valid_function():
    fn = FunctionCreate(name="my-fn", gitRepo="https://git/x.git", gitToken="t", runtime="python")
    assert fn.branch == "main"
    assert fn.scaling.minScale == 0


def test_invalid_name_rejected():
    with pytest.raises(ValidationError):
        FunctionCreate(name="Bad_Name", gitRepo="g", gitToken="t", runtime="python")


def test_runtime_is_a_free_string_on_the_model():
    # The valid runtime set is data (a mounted ConfigMap), so the model accepts
    # any string; the service validates it against the live registry (see
    # test_auth_and_deployer.test_function_accept_rejects_unknown_runtime).
    fn = FunctionCreate(name="x", gitRepo="g", gitToken="t", runtime="ruby")
    assert fn.runtime == "ruby"


def test_size_default_and_choices():
    fn = FunctionCreate(name="x", gitRepo="g", gitToken="t", runtime="go")
    assert fn.size == "small"  # default
    assert (
        FunctionCreate(name="x", gitRepo="g", gitToken="t", runtime="go", size="large").size
        == "large"
    )
    with pytest.raises(ValidationError):  # unknown size
        FunctionCreate(name="x", gitRepo="g", gitToken="t", runtime="go", size="xl")


def test_envvar_value_required_unless_secret_keep():
    # a non-secret var always needs a value
    with pytest.raises(ValidationError):
        EnvVar(name="X")
    assert EnvVar(name="X", value="1").secret is False
    # a secret var MAY omit the value -> "keep the stored value" on update
    keep = EnvVar(name="X", secret=True)
    assert keep.secret is True and keep.value is None
    assert EnvVar(name="X", value="1", secret=True).value == "1"


def test_filemount_content_required_unless_secret_keep():
    f = FileMount(mountPath="/etc/a", content="hi")
    assert f.readOnly is True and f.keep is False
    assert FileMount(mountPath="/etc/a", content="hi", readOnly=False).readOnly is False
    # a secret file MAY omit content -> "keep the stored content" on update
    keep = FileMount(mountPath="/etc/a", secret=True)
    assert keep.keep is True
    # a non-secret file still needs exactly one content field
    with pytest.raises(ValidationError):
        FileMount(mountPath="/etc/a")  # no content, not secret
    # supplying both is always rejected, even for a secret
    with pytest.raises(ValidationError):
        FileMount(mountPath="/etc/a", content="x", contentBase64="eA==", secret=True)


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


def test_scaling_scale_down_delay():
    # Accepts single-unit durations up to 1h.
    for good in ("0s", "30s", "5m", "1h", "60m", "3600s"):
        assert Scaling(scaleDownDelay=good).scaleDownDelay == good
    # Bad format or over the 1h cap is rejected.
    for bad in ("5", "5min", "1h30m", "-5s", "2h", "61m", "3601s"):
        with pytest.raises(ValidationError):
            Scaling(scaleDownDelay=bad)


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
        gitRepo="g",
        gitToken="t",
        runtime="python",
        hostname="app.example.com",
    )
    assert fn.hostname == "app.example.com"
    # default (no hostname) is allowed
    assert FunctionCreate(name="x", gitRepo="g", gitToken="t", runtime="go").hostname is None
    # invalid hostnames rejected
    for bad in ["NoDots", "UPPER.example.com", "bad_host.example.com"]:
        with pytest.raises(ValidationError):
            FunctionCreate(name="x", gitRepo="g", gitToken="t", runtime="go", hostname=bad)
