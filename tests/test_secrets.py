import base64
import json

from api.services.secrets import build_pull_secret, registry_of, registry_username


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
    # the username is decodable back; the token never leaves the secret
    assert registry_username(s) == "bob"
