from app.models.common import Scaling
from app.services.describe import configmap_refs, parse_spec
from app.services.files import VolumeSpec
from app.services.ksvc import ContainerEnv, build_ksvc


def _ksvc():
    return build_ksvc(
        name="app-team",
        group="team",
        owner="alice",
        image="reg/app:1",
        offering="container",
        host="app-team.ex.com",
        env=[
            ContainerEnv(name="LOG", value="debug"),
            ContainerEnv(name="API_KEY", secret_ref=("app-team-env", "API_KEY")),
        ],
        volumes=[
            VolumeSpec("files-config", "configmap", "app-team-files", "/etc/app.conf", "etc-app.conf", True),
            VolumeSpec("files-secret", "secret", "app-team-files", "/etc/secret", "etc-secret", True),
        ],
        scaling=Scaling(minScale=1, maxScale=4, metric="cpu", target=80),
        size="medium",
        pull_secret="app-team-pull",
        ca_config_map="trusted-ca",
        ca_mount_path="/etc/pki/tls/certs/ca.crt",
    )


def test_configmap_refs_excludes_platform_ca():
    assert configmap_refs(_ksvc()) == {"app-team-files"}


def test_parse_spec_redacts_secrets_and_returns_plain_config():
    spec = parse_spec(
        _ksvc(),
        {"app-team-files": {"etc-app.conf": "level=debug"}},
        registry_username="bob",
    )

    assert spec.scaling.metric == "cpu"
    assert spec.scaling.effective_target == 80
    assert spec.scaling.minScale == 1 and spec.scaling.maxScale == 4

    envs = {e.name: e for e in spec.env}
    assert envs["LOG"].value == "debug" and envs["LOG"].secret is False
    # secret-backed value never returned
    assert envs["API_KEY"].secret is True and envs["API_KEY"].value is None

    files = {f.mountPath: f for f in spec.files}
    assert files["/etc/app.conf"].secret is False
    assert files["/etc/app.conf"].content == "level=debug"  # plain content filled in
    # secret file content always redacted
    assert files["/etc/secret"].secret is True and files["/etc/secret"].content is None

    # registry username shown; token never part of the spec
    assert spec.registryUsername == "bob"


def test_parse_spec_reports_function_build_inputs():
    from app.models.common import Scaling as _Scaling

    ksvc = build_ksvc(
        name="fn-team", group="team", owner="alice", image="reg/fn:main",
        offering="function", host="fn-team.ex.com", env=[], volumes=[],
        scaling=_Scaling(), size="small",
        runtime="python", git_url="https://git.example.com/app.git", branch="release",
    )
    spec = parse_spec(ksvc)
    assert spec.gitUrl == "https://git.example.com/app.git"
    assert spec.branch == "release"
    assert spec.registryUsername is None  # functions have no pull secret


def test_parse_spec_without_configmap_leaves_content_null():
    spec = parse_spec(_ksvc())  # no configmaps provided
    plain = next(f for f in spec.files if f.mountPath == "/etc/app.conf")
    assert plain.content is None  # best-effort: not fetched -> null, not an error
