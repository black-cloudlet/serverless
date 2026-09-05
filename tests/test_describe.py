from api.models.common import Scaling
from api.services.manifests.files import VolumeSpec
from api.services.manifests.ksvc import ContainerEnv, build_ksvc
from api.services.state.describe import configmap_refs, parse_spec


def _ksvc():
    return build_ksvc(
        name="app",
        group="team",
        owner="alice",
        image="reg/app:1",
        offering="container",
        host="app-team.ex.com",
        env=[
            ContainerEnv(name="LOG", value="debug"),
            ContainerEnv(name="API_KEY", secret_ref=("app-env", "API_KEY")),
        ],
        volumes=[
            VolumeSpec("files-config", "configmap", "app-files", "/etc/app.conf", "etc-app.conf"),
            VolumeSpec("files-secret", "secret", "app-files", "/etc/secret", "etc-secret"),
        ],
        scaling=Scaling(minScale=1, maxScale=4, metric="cpu", target=80),
        size="medium",
        pull_secret="app-pull",
        ca_config_map="trusted-ca",
        ca_mount_path="/etc/pki/tls/certs/ca.crt",
    )


def test_configmap_refs_excludes_platform_ca():
    assert configmap_refs(_ksvc()) == {"app-files"}


def test_parse_spec_hides_injected_ca_env_but_keeps_user_override():
    from api.services.manifests.ksvc import CA_ENV_VARS

    # The user sets SSL_CERT_FILE themselves; the rest are platform-injected.
    ksvc = build_ksvc(
        name="app",
        group="team",
        owner="alice",
        image="reg/app:1",
        offering="container",
        host="app-team.ex.com",
        env=[
            ContainerEnv(name="LOG", value="debug"),
            ContainerEnv(name="SSL_CERT_FILE", value="/custom/ca.pem"),
        ],
        volumes=[],
        scaling=Scaling(),
        ca_config_map="ca-bundle",
        ca_mount_path="/etc/ssl/certs",
        ca_file="/etc/ssl/certs/ca-bundle.crt",
    )
    names = {e.name for e in parse_spec(ksvc).env}
    # transparent defaults are hidden; the user's own vars (incl. the override) show
    assert names == {"LOG", "SSL_CERT_FILE"}
    assert (set(CA_ENV_VARS) - {"SSL_CERT_FILE"}).isdisjoint(names)
    envs = {e.name: e for e in parse_spec(ksvc).env}
    assert envs["SSL_CERT_FILE"].value == "/custom/ca.pem"  # user value preserved


def test_parse_spec_redacts_secrets_and_returns_plain_config():
    spec = parse_spec(
        _ksvc(),
        {"app-files": {"etc-app.conf": "level=debug"}},
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
    from api.models.common import Scaling as _Scaling

    ksvc = build_ksvc(
        name="fn",
        group="team",
        owner="alice",
        image="reg/fn:main",
        offering="function",
        host="fn-team.ex.com",
        env=[],
        volumes=[],
        scaling=_Scaling(),
        size="small",
        runtime="python",
        git_url="https://git.example.com/app.git",
        revision="release",
        path="services/api",
    )
    spec = parse_spec(ksvc)
    assert spec.gitRepo == "https://git.example.com/app.git"
    assert spec.revision == "release"
    assert spec.path == "services/api"
    assert spec.registryUsername is None  # functions have no pull secret


def test_parse_spec_returns_binary_configmap_content_as_base64():
    import base64

    # region_read hands binaryData entries over as bytes; the view reports them
    # base64-encoded with encoding "base64", mirroring how they are submitted.
    blob = bytes([0xFF, 0xFE, 0x00])
    spec = parse_spec(_ksvc(), {"app-files": {"etc-app.conf": blob}})
    plain = next(f for f in spec.files if f.mountPath == "/etc/app.conf")
    assert plain.encoding == "base64"
    assert base64.b64decode(plain.content) == blob
    # text entries stay str -> text encoding
    spec = parse_spec(_ksvc(), {"app-files": {"etc-app.conf": "level=debug"}})
    plain = next(f for f in spec.files if f.mountPath == "/etc/app.conf")
    assert plain.encoding == "text" and plain.content == "level=debug"


def test_parse_spec_without_configmap_leaves_content_null():
    spec = parse_spec(_ksvc())  # no configmaps provided
    plain = next(f for f in spec.files if f.mountPath == "/etc/app.conf")
    assert plain.content is None  # best-effort: not fetched -> null, not an error


def test_parse_spec_reads_back_container_port():
    from api.services.state.describe import container_port

    # container_port stays honest about the manifest: nothing stamped -> None
    assert container_port(_ksvc()) is None
    # ...but the spec reports the port the workload actually serves on. Only a
    # workload written before ports were always stamped reaches this.
    assert parse_spec(_ksvc()).port == 8080
    # an explicit port round-trips through the KSVC
    ksvc = build_ksvc(
        name="app",
        group="team",
        owner="alice",
        image="reg/app:1",
        offering="container",
        host="app-team.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        port=9000,
    )
    assert container_port(ksvc) == 9000
    assert parse_spec(ksvc).port == 9000
