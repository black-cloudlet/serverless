import base64
import json

from api.services.manifests.secrets import (
    GIT_TOKEN_KEY,
    build_git_secret,
    build_pull_secret,
    git_secret_name,
    registry_of,
    registry_token,
    registry_username,
)


def test_registry_of():
    assert registry_of("reg.example.com/team/app:1") == "reg.example.com"
    assert registry_of("reg.example.com:5000/app@sha256:abc") == "reg.example.com:5000"
    assert registry_of("localhost/app") == "localhost"
    # no explicit registry -> Docker Hub
    assert registry_of("nginx:latest") == "docker.io"
    assert registry_of("team/app:tag") == "docker.io"


def test_build_pull_secret_keys_on_registry_and_decodes_creds():
    s = build_pull_secret("p", {}, "reg.example.com", "bob", "s3cret")
    cfg = json.loads(base64.b64decode(s["data"][".dockerconfigjson"]))
    # keyed to the given registry host
    assert set(cfg["auths"]) == {"reg.example.com"}
    assert cfg["auths"]["reg.example.com"]["username"] == "bob"
    # both fields decode back (username shown on read; token used internally only,
    # to re-key the pull secret when the credential is kept)
    assert registry_username(s) == "bob"
    assert registry_token(s) == "s3cret"


def test_registry_field_decode_handles_missing_secret():
    assert registry_username({}) is None
    assert registry_token({}) is None
    assert registry_token({"data": {".dockerconfigjson": "not-base64!!"}}) is None


def test_git_secret_roundtrips_token():
    name = git_secret_name("app")
    assert name == "app-git"
    s = build_git_secret(name, {"app": "x"}, "ghp_secret")
    # basic-auth, not Opaque: kpack clones with this same Secret
    assert s["kind"] == "Secret" and s["type"] == "kubernetes.io/basic-auth"
    assert s["metadata"]["name"] == name and s["metadata"]["labels"] == {"app": "x"}
    # stored base64-encoded; load_existing decodes it back to reuse on rebuild
    assert base64.b64decode(s["data"][GIT_TOKEN_KEY]).decode() == "ghp_secret"
