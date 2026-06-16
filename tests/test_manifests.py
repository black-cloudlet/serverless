from app.models.common import (
    LABEL_GROUP,
    LABEL_OFFERING,
    EnvVar,
    FileMount,
    Scaling,
    ValueFrom,
)
from app.services import ksvc as ksvc_svc
from app.services import resources as res
from app.services import route as route_svc
from app.services import secrets as secret_svc
from app.services.files import resolve_files


def test_build_ksvc_basic():
    files = resolve_files("app", "team", "alice", [])
    m = ksvc_svc.build_ksvc(
        name="app",
        group="team",
        owner="alice",
        image="reg/x:1",
        offering="caas",
        env=[EnvVar(name="LOG", value="info")],
        volumes=files.volumes,
        scaling=Scaling(minScale=1, maxScale=3, targetConcurrency=50),
        pull_secret="app-pull",
    )
    assert m["apiVersion"] == "serving.knative.dev/v1"
    assert m["metadata"]["labels"][LABEL_GROUP] == "team"
    tmpl = m["spec"]["template"]
    assert tmpl["metadata"]["annotations"]["autoscaling.knative.dev/min-scale"] == "1"
    assert tmpl["metadata"]["annotations"]["autoscaling.knative.dev/target"] == "50"
    spec = tmpl["spec"]
    assert spec["imagePullSecrets"] == [{"name": "app-pull"}]
    assert spec["containers"][0]["env"] == [{"name": "LOG", "value": "info"}]


def test_build_ksvc_env_valuefrom():
    m = ksvc_svc.build_ksvc(
        name="app",
        group="t",
        owner="o",
        image="i",
        offering="faas",
        env=[EnvVar(name="P", valueFrom=ValueFrom(secret="s", key="k"))],
        volumes=[],
        scaling=Scaling(),
    )
    env = m["spec"]["template"]["spec"]["containers"][0]["env"][0]
    assert env["valueFrom"]["secretKeyRef"] == {"name": "s", "key": "k"}


def test_resolve_inline_file_creates_backing_and_mount():
    files = [FileMount(mountPath="/etc/app/app.yaml", content="x: 1\n")]
    resolved = resolve_files("app", "team", "alice", files)
    assert len(resolved.backing) == 1
    cm = resolved.backing[0]
    assert cm["kind"] == "ConfigMap"
    assert cm["data"] == {"app.yaml": "x: 1\n"}
    vol = resolved.volumes[0]
    assert vol.kind == "configmap"
    assert vol.sub_path == "app.yaml"
    assert vol.mount_path == "/etc/app/app.yaml"


def test_resolve_inline_secret_file():
    files = [FileMount(mountPath="/etc/tls/tls.key", content="KEY", secret=True)]
    resolved = resolve_files("app", "team", "alice", files)
    assert resolved.backing[0]["kind"] == "Secret"
    assert resolved.volumes[0].kind == "secret"


def test_resolve_source_reference_no_backing():
    files = [FileMount(mountPath="/etc/app", source="cfg", type="configmap")]
    resolved = resolve_files("app", "team", "alice", files)
    assert resolved.backing == []
    assert resolved.volumes[0].source_name == "cfg"
    assert resolved.volumes[0].sub_path is None


def test_host_and_route():
    host = route_svc.host_for("app", "team", "serverless.example.com")
    assert host == "app-team.serverless.example.com"
    r = route_svc.build_route(
        name="app",
        group="team",
        owner="o",
        offering="caas",
        host=host,
        target_namespace="serverless-workloads",
    )
    assert r["spec"]["host"] == host
    assert r["metadata"]["labels"][LABEL_OFFERING] == "caas"
    dm = route_svc.build_domain_mapping(
        name="app", group="team", owner="o", offering="caas", host=host
    )
    assert dm["metadata"]["name"] == host
    assert dm["spec"]["ref"]["name"] == "app"


def test_pull_secret_dockerconfig():
    import base64
    import json

    s = secret_svc.build_pull_secret(
        "p", "team", "o", "registry.internal", "user", "tok"
    )
    assert s["type"] == "kubernetes.io/dockerconfigjson"
    cfg = json.loads(base64.b64decode(s["data"][".dockerconfigjson"]))
    assert "registry.internal" in cfg["auths"]


def test_build_secret_encodes_values():
    import base64

    s = res.build_secret("n", "team", "o", {"k": "v"})
    assert base64.b64decode(s["data"]["k"]).decode() == "v"
