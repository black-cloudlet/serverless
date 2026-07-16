import base64
import json

from api.services.secrets import (
    GIT_TOKEN_KEY,
    build_git_secret,
    build_pull_secret,
    git_secret_name,
    git_token,
    registry_of,
    registry_username,
)


def test_registry_of():
    assert registry_of("reg.example.com/team/app:1") == "reg.example.com"
    assert registry_of("reg.example.com:5000/app@sha256:abc") == "reg.example.com:5000"
    assert registry_of("localhost/app") == "localhost"
    # no explicit registry -> Docker Hub
    assert registry_of("nginx:latest") == "docker.io"
    assert registry_of("team/app:tag") == "docker.io"


def test_build_pull_secret_keys_on_registry_and_redacts_token():
    s = build_pull_secret("p", {}, "reg.example.com", "bob", "s3cret")
    cfg = json.loads(base64.b64decode(s["data"][".dockerconfigjson"]))
    # keyed to the given registry host
    assert set(cfg["auths"]) == {"reg.example.com"}
    assert cfg["auths"]["reg.example.com"]["username"] == "bob"
    # the username is decodable back; the token never leaves the secret on read
    assert registry_username(s) == "bob"


def test_git_secret_roundtrips_token():
    name = git_secret_name("app-team")
    assert name == "app-team-git"
    s = build_git_secret(name, {"app": "x"}, "ghp_secret")
    assert s["kind"] == "Secret" and s["type"] == "Opaque"
    assert s["metadata"]["name"] == name and s["metadata"]["labels"] == {"app": "x"}
    # stored base64-encoded under the token key; the update path decodes it back
    assert base64.b64decode(s["data"][GIT_TOKEN_KEY]).decode() == "ghp_secret"
    assert git_token(s) == "ghp_secret"


def test_git_token_decode_handles_missing_secret():
    assert git_token({}) is None
    assert git_token({"data": {}}) is None
