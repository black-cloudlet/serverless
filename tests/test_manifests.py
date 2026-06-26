from app.models.common import (
    LABEL_GROUP,
    LABEL_OFFERING,
    EnvVar,
    FileMount,
    Scaling,
)
from app.services import ksvc as ksvc_svc
from app.services import resources as res
from app.services import route as route_svc
from app.services import secrets as secret_svc
from app.services.env import env_secret_name, resolve_env
from app.services.files import resolve_files
from app.services.ksvc import ContainerEnv


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
    assert m["metadata"]["annotations"]["serverless.platform/host"] == "app-team.serverless.example.com"
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
        ca_config_map="trusted-ca-bundle",
        ca_mount_path="/etc/serverless/trusted-ca",
    )
    spec = m["spec"]["template"]["spec"]
    assert {"name": "trusted-ca", "configMap": {"name": "trusted-ca-bundle"}} in spec["volumes"]
    mount = spec["containers"][0]["volumeMounts"][0]
    assert mount["mountPath"] == "/etc/serverless/trusted-ca"
    assert mount["readOnly"] is True


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
    from app.models.common import LABEL_GROUP, LABEL_WORKLOAD
    from app.services.files import files_name

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

    from app.core.errors import ValidationError

    files = [
        FileMount(mountPath="/a/conf", content="1"),
        FileMount(mountPath="/a/conf", content="2"),
    ]
    with pytest.raises(ValidationError):  # 400, not a raw ValueError (500)
        resolve_files("app", "team", "alice", files)


def test_resolve_files_invalid_base64_rejected():
    import pytest

    from app.core.errors import ValidationError

    # "abc" has incorrect padding -> binascii.Error (a ValueError) even on a
    # lenient decode, so it surfaces as a 400.
    files = [FileMount(mountPath="/a/conf", contentBase64="abc")]
    with pytest.raises(ValidationError):
        resolve_files("app", "team", "alice", files)


def test_resolve_files_accepts_linewrapped_base64():
    import base64

    from app.models.common import FileMount

    # PEM-style line-wrapped base64 (newlines) must still decode, not 400.
    wrapped = base64.encodebytes(b"hello world, this is a longer body") .decode()
    resolved = resolve_files(
        "app", "team", "alice", [FileMount(mountPath="/a/conf", contentBase64=wrapped)]
    )
    cm = next(b for b in resolved.backing if b["kind"] == "ConfigMap")
    assert "hello world" in next(iter(cm["data"].values()))


def test_resolve_env_duplicate_name_rejected():
    import pytest

    from app.core.errors import ValidationError
    from app.models.common import EnvVar
    from app.services.env import resolve_env

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

    from app.services.labels import workload_labels

    labels = workload_labels("team", "o", "app", "container")
    s = secret_svc.build_pull_secret(
        "p", labels, "registry.internal", "user", "tok"
    )
    assert s["type"] == "kubernetes.io/dockerconfigjson"
    assert s["metadata"]["labels"][LABEL_GROUP] == "team"
    cfg = json.loads(base64.b64decode(s["data"][".dockerconfigjson"]))
    assert "registry.internal" in cfg["auths"]


def test_build_secret_encodes_values():
    import base64

    from app.services.labels import ownership_labels

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
