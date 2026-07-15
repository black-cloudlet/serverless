from api.models.common import (
    LABEL_GROUP,
    LABEL_OFFERING,
    EnvVar,
    FileMount,
    Scaling,
)
from api.services import ksvc as ksvc_svc
from api.services import resources as res
from api.services import route as route_svc
from api.services import secrets as secret_svc
from api.services.env import env_secret_name, resolve_env
from api.services.files import resolve_files
from api.services.ksvc import ContainerEnv


def test_build_ksvc_basic():
    files = resolve_files("app", "team", "alice", [])
    m = ksvc_svc.build_ksvc(
        name="app-team",
        group="team",
        owner="alice",
        image="reg/x:1",
        offering="container",
        host="app-team.serverless.example.com",
        env=[ContainerEnv(name="LOG", value="info")],
        volumes=files.volumes,
        scaling=Scaling(minScale=1, maxScale=3, target=50),
        pull_secret="app-pull",
    )
    assert m["apiVersion"] == "serving.knative.dev/v1"
    assert m["metadata"]["name"] == "app-team"
    assert (
        m["metadata"]["annotations"]["serverless.platform/host"]
        == "app-team.serverless.example.com"
    )
    assert m["metadata"]["labels"][LABEL_GROUP] == "team"
    tmpl = m["spec"]["template"]
    ann = tmpl["metadata"]["annotations"]
    assert ann["autoscaling.knative.dev/min-scale"] == "1"
    assert ann["autoscaling.knative.dev/target"] == "50"
    # default metric is concurrency (KPA) -> no class annotation
    assert ann["autoscaling.knative.dev/metric"] == "concurrency"
    assert "autoscaling.knative.dev/class" not in ann
    spec = tmpl["spec"]
    assert spec["imagePullSecrets"] == [{"name": "app-pull"}]
    assert spec["containers"][0]["env"] == [{"name": "LOG", "value": "info"}]


def test_build_ksvc_size_sets_resources():
    m = ksvc_svc.build_ksvc(
        name="app-team",
        group="team",
        owner="o",
        image="i",
        offering="container",
        host="app-team.serverless.example.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="medium",
    )
    res = m["spec"]["template"]["spec"]["containers"][0]["resources"]
    # memory is request==limit (hard cap); cpu is request-only (no limit).
    assert res["requests"] == {"cpu": "250m", "memory": "512Mi"}
    assert res["limits"] == {"memory": "512Mi"}
    assert "cpu" not in res["limits"]


def test_build_ksvc_cpu_metric_sets_hpa_class():
    m = ksvc_svc.build_ksvc(
        name="app-team",
        group="team",
        owner="o",
        image="i",
        offering="container",
        host="app-team.serverless.example.com",
        env=[],
        volumes=[],
        scaling=Scaling(minScale=1, maxScale=5, metric="cpu", target=70),
    )
    ann = m["spec"]["template"]["metadata"]["annotations"]
    assert ann["autoscaling.knative.dev/metric"] == "cpu"
    assert ann["autoscaling.knative.dev/target"] == "70"
    assert ann["autoscaling.knative.dev/class"] == "hpa.autoscaling.knative.dev"


def test_build_ksvc_scale_down_delay_annotation():
    common = dict(
        name="app-team",
        group="team",
        owner="o",
        image="i",
        offering="container",
        host="app-team.serverless.example.com",
        env=[],
        volumes=[],
    )
    # Set -> annotation present; unset -> annotation omitted.
    m = ksvc_svc.build_ksvc(scaling=Scaling(minScale=1, maxScale=3, scaleDownDelay="5m"), **common)
    ann = m["spec"]["template"]["metadata"]["annotations"]
    assert ann["autoscaling.knative.dev/scale-down-delay"] == "5m"
    m2 = ksvc_svc.build_ksvc(scaling=Scaling(minScale=1, maxScale=3), **common)
    assert (
        "autoscaling.knative.dev/scale-down-delay"
        not in m2["spec"]["template"]["metadata"]["annotations"]
    )


def test_build_ksvc_mounts_ca_bundle():
    m = ksvc_svc.build_ksvc(
        name="app-team",
        group="team",
        owner="o",
        image="i",
        offering="container",
        host="app-team.serverless.example.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        ca_config_map="ca-bundle",
        ca_mount_path="/etc/ssl/certs",
    )
    spec = m["spec"]["template"]["spec"]
    assert {"name": "ca-bundle", "configMap": {"name": "ca-bundle"}} in spec["volumes"]
    mount = spec["containers"][0]["volumeMounts"][0]
    assert mount["mountPath"] == "/etc/ssl/certs"
    assert mount["readOnly"] is True


def test_build_ksvc_injects_ca_env_pointed_at_the_bundle():
    from api.models.common import ANNOTATION_INJECTED_ENV
    from api.services.ksvc import CA_ENV_VARS

    m = ksvc_svc.build_ksvc(
        name="app-team",
        group="team",
        owner="o",
        image="i",
        offering="container",
        host="app-team.serverless.example.com",
        env=[ContainerEnv(name="LOG", value="info")],
        volumes=[],
        scaling=Scaling(),
        ca_config_map="ca-bundle",
        ca_mount_path="/etc/ssl/certs",
        ca_file="/etc/ssl/certs/ca-bundle.crt",
    )
    env = {e["name"]: e for e in m["spec"]["template"]["spec"]["containers"][0]["env"]}
    # the user's var is kept, and every CA-trust var points at the bundle file
    assert env["LOG"] == {"name": "LOG", "value": "info"}
    for var in CA_ENV_VARS:
        assert env[var] == {"name": var, "value": "/etc/ssl/certs/ca-bundle.crt"}
    # injected names are recorded so read-back can hide them
    injected = m["metadata"]["annotations"][ANNOTATION_INJECTED_ENV].split(",")
    assert set(injected) == set(CA_ENV_VARS)


def test_build_ksvc_ca_env_user_override_wins_and_is_not_marked_injected():
    from api.models.common import ANNOTATION_INJECTED_ENV
    from api.services.ksvc import CA_ENV_VARS

    m = ksvc_svc.build_ksvc(
        name="app-team",
        group="team",
        owner="o",
        image="i",
        offering="container",
        host="app-team.serverless.example.com",
        env=[ContainerEnv(name="SSL_CERT_FILE", value="/custom/ca.pem")],
        volumes=[],
        scaling=Scaling(),
        ca_config_map="ca-bundle",
        ca_mount_path="/etc/ssl/certs",
        ca_file="/etc/ssl/certs/ca-bundle.crt",
    )
    env = {e["name"]: e for e in m["spec"]["template"]["spec"]["containers"][0]["env"]}
    # the user's value wins and appears once (not overwritten by the default)
    assert env["SSL_CERT_FILE"] == {"name": "SSL_CERT_FILE", "value": "/custom/ca.pem"}
    injected = m["metadata"]["annotations"][ANNOTATION_INJECTED_ENV].split(",")
    assert "SSL_CERT_FILE" not in injected  # user-set -> never marked injected
    assert set(injected) == set(CA_ENV_VARS) - {"SSL_CERT_FILE"}


def test_build_ksvc_no_ca_env_without_ca_file():
    from api.models.common import ANNOTATION_INJECTED_ENV

    # No ca_file -> no injection, no injected-env annotation (mount still added).
    m = ksvc_svc.build_ksvc(
        name="app-team",
        group="team",
        owner="o",
        image="i",
        offering="container",
        host="app-team.serverless.example.com",
        env=[ContainerEnv(name="LOG", value="info")],
        volumes=[],
        scaling=Scaling(),
        ca_config_map="ca-bundle",
        ca_mount_path="/etc/ssl/certs",
    )
    env = m["spec"]["template"]["spec"]["containers"][0]["env"]
    assert env == [{"name": "LOG", "value": "info"}]
    assert ANNOTATION_INJECTED_ENV not in m["metadata"]["annotations"]


def test_build_ksvc_env_secret_ref():
    m = ksvc_svc.build_ksvc(
        name="app-t",
        group="t",
        owner="o",
        image="i",
        offering="function",
        host="app-t.serverless.example.com",
        env=[ContainerEnv(name="P", secret_ref=("s", "k"))],
        volumes=[],
        scaling=Scaling(),
    )
    env = m["spec"]["template"]["spec"]["containers"][0]["env"][0]
    assert env["valueFrom"]["secretKeyRef"] == {"name": "s", "key": "k"}


def test_resolve_files_aggregates_one_cm_and_one_secret():
    from api.models.common import LABEL_GROUP, LABEL_WORKLOAD
    from api.services.files import files_name

    files = [
        FileMount(mountPath="/etc/app/app.yaml", content="x: 1\n"),
        FileMount(mountPath="/etc/app/extra.conf", content="a=b\n", readOnly=False),
        FileMount(mountPath="/etc/tls/tls.key", content="KEY", secret=True),
    ]
    resolved = resolve_files("app", "team", "alice", files)

    # exactly one ConfigMap + one Secret
    kinds = sorted(b["kind"] for b in resolved.backing)
    assert kinds == ["ConfigMap", "Secret"]
    by_kind = {b["kind"]: b for b in resolved.backing}

    cm = by_kind["ConfigMap"]
    assert cm["metadata"]["name"] == files_name("app")
    assert set(cm["data"]) == {"etc-app-app.yaml", "etc-app-extra.conf"}
    # every workload resource carries group + workload labels
    assert cm["metadata"]["labels"][LABEL_GROUP] == "team"
    assert cm["metadata"]["labels"][LABEL_WORKLOAD] == "app"

    sec = by_kind["Secret"]
    assert set(sec["data"]) == {"etc-tls-tls.key"}

    # volumes share volume names per kind; mounts carry subPath + readOnly
    cfg_vols = [v for v in resolved.volumes if v.kind == "configmap"]
    assert {v.volume_name for v in cfg_vols} == {"files-config"}
    rw = next(v for v in resolved.volumes if v.mount_path == "/etc/app/extra.conf")
    assert rw.read_only is False


def test_resolve_files_duplicate_key_rejected():
    import pytest

    from common.errors import ValidationError

    files = [
        FileMount(mountPath="/a/conf", content="1"),
        FileMount(mountPath="/a/conf", content="2"),
    ]
    with pytest.raises(ValidationError):  # 400, not a raw ValueError (500)
        resolve_files("app", "team", "alice", files)


def test_resolve_files_invalid_base64_rejected():
    import pytest

    from common.errors import ValidationError

    # "abc" has incorrect padding -> binascii.Error (a ValueError) even on a
    # lenient decode, so it surfaces as a 400.
    files = [FileMount(mountPath="/a/conf", contentBase64="abc")]
    with pytest.raises(ValidationError):
        resolve_files("app", "team", "alice", files)


def test_resolve_files_accepts_linewrapped_base64():
    import base64

    from api.models.common import FileMount

    # PEM-style line-wrapped base64 (newlines) must still decode, not 400.
    wrapped = base64.encodebytes(b"hello world, this is a longer body").decode()
    resolved = resolve_files(
        "app", "team", "alice", [FileMount(mountPath="/a/conf", contentBase64=wrapped)]
    )
    cm = next(b for b in resolved.backing if b["kind"] == "ConfigMap")
    assert "hello world" in next(iter(cm["data"].values()))


def test_resolve_env_duplicate_name_rejected():
    import pytest

    from api.models.common import EnvVar
    from api.services.env import resolve_env
    from common.errors import ValidationError

    env = [EnvVar(name="DUP", value="1"), EnvVar(name="DUP", value="2")]
    with pytest.raises(ValidationError):  # surfaced synchronously as 400
        resolve_env("app", "team", "alice", env)


def test_host_and_domain_mapping():
    host = route_svc.host_for("app", "team", "serverless.example.com")
    assert host == "app-team.serverless.example.com"
    dm = route_svc.build_domain_mapping(
        name="app", group="team", owner="o", offering="container", host=host
    )
    assert dm["apiVersion"] == "serving.knative.dev/v1beta1"
    assert dm["metadata"]["name"] == host
    assert dm["metadata"]["labels"][LABEL_OFFERING] == "container"
    assert dm["spec"]["ref"]["name"] == "app"


def test_pull_secret_dockerconfig():
    import base64
    import json

    from common.labels import workload_labels

    labels = workload_labels("team", "o", "app", "container")
    s = secret_svc.build_pull_secret("p", labels, "registry.internal", "user", "tok")
    assert s["type"] == "kubernetes.io/dockerconfigjson"
    assert s["metadata"]["labels"][LABEL_GROUP] == "team"
    cfg = json.loads(base64.b64decode(s["data"][".dockerconfigjson"]))
    assert "registry.internal" in cfg["auths"]


def test_build_secret_encodes_values():
    import base64

    from common.labels import ownership_labels

    s = res.build_secret("n", ownership_labels("team", "o"), {"k": "v"})
    assert base64.b64decode(s["data"]["k"]).decode() == "v"


def test_resolve_env_plain_only_no_secret():
    resolved = resolve_env("app", "team", "o", [EnvVar(name="LOG", value="info")])
    assert resolved.backing == []
    assert resolved.env[0].value == "info"
    assert resolved.env[0].secret_ref is None


def test_resolve_env_secret_creates_secret_and_rewrites_ref():
    import base64

    env = [
        EnvVar(name="LOG", value="info"),
        EnvVar(name="DB_PASSWORD", value="s3cret", secret=True),
    ]
    resolved = resolve_env("app", "team", "alice", env)

    # one backing Secret holding the secret value
    assert len(resolved.backing) == 1
    sec = resolved.backing[0]
    assert sec["kind"] == "Secret"
    assert sec["metadata"]["name"] == env_secret_name("app")
    assert base64.b64decode(sec["data"]["DB_PASSWORD"]).decode() == "s3cret"

    # plain env stays a literal; secret env becomes a secretKeyRef
    by_name = {e.name: e for e in resolved.env}
    assert by_name["LOG"].value == "info"
    assert by_name["LOG"].secret_ref is None
    assert by_name["DB_PASSWORD"].value is None
    assert by_name["DB_PASSWORD"].secret_ref == (env_secret_name("app"), "DB_PASSWORD")
