"""Resolving one region's registry out of the platform default plus its override."""

from __future__ import annotations

from common.config import CommonSettings, RegionConfig, RegionRegistry


def _settings(**over):
    base = dict(
        regions=[
            RegionConfig(name="central", cluster="central-0"),
            RegionConfig(name="south", cluster="south-0"),
        ],
        registry={"url": "registry.internal", "organization": "acme", "repository": "serverless"},
    )
    base.update(over)
    return CommonSettings(**base)


def test_region_without_an_override_takes_the_platform_default():
    reg = _settings().registry_for("central")
    assert reg.url == "registry.internal"
    assert reg.base == "registry.internal/acme/serverless"


def test_unknown_region_takes_the_platform_default():
    # Not a fallback for a typo so much as the normal single-registry install,
    # where no region names a registry at all.
    assert _settings().registry_for("nowhere").base == "registry.internal/acme/serverless"


def test_override_moves_the_host_and_inherits_the_path():
    settings = _settings(
        regions=[
            RegionConfig(
                name="central",
                cluster="central-0",
                registry=RegionRegistry(url="registry.central.internal"),
            ),
            RegionConfig(name="south", cluster="south-0"),
        ]
    )
    assert settings.registry_for("central").base == "registry.central.internal/acme/serverless"
    # The peer is untouched by its neighbour's override.
    assert settings.registry_for("south").base == "registry.internal/acme/serverless"


def test_empty_string_overrides_the_path_but_none_inherits_it():
    # The distinction the RegionRegistry docstring turns on: "" is how an install
    # says a registry has no namespacing path, so it cannot mean "inherit".
    settings = _settings(
        regions=[
            RegionConfig(
                name="central",
                cluster="central-0",
                registry=RegionRegistry(url="registry.central.internal", organization=""),
            )
        ]
    )
    assert settings.registry_for("central").base == "registry.central.internal/serverless"


def test_override_can_replace_every_segment():
    settings = _settings(
        regions=[
            RegionConfig(
                name="central",
                cluster="central-0",
                registry=RegionRegistry(
                    url="registry.central.internal", organization="other", repository="fn"
                ),
            )
        ]
    )
    assert settings.registry_for("central").base == "registry.central.internal/other/fn"


def test_resolving_does_not_mutate_the_platform_default():
    settings = _settings(
        regions=[
            RegionConfig(
                name="central",
                cluster="central-0",
                registry=RegionRegistry(url="registry.central.internal"),
            )
        ]
    )
    settings.registry_for("central")
    assert settings.registry.url == "registry.internal"


def test_per_region_api_token_wins_and_a_named_region_keeps_its_own():
    settings = _settings(region_registry_tokens={"central": "tok-central", "south": "tok-south"})
    assert settings.registry_for("central").api_token == "tok-central"
    assert settings.registry_for("south").api_token == "tok-south"


def test_a_region_the_token_map_omits_falls_back_to_the_platform_token():
    # An empty token disables cleanup outright (`can_delete`), so an omitted
    # region must not resolve to "" and silently stop reclaiming repositories.
    settings = _settings(
        registry={"url": "registry.internal", "api_token": "tok-platform"},
        region_registry_tokens={"central": "tok-central"},
    )
    assert settings.registry_for("south").api_token == "tok-platform"
    assert settings.registry_for("south").can_delete


def test_the_token_fallback_compares_canonical_hosts_not_spellings():
    # The same registry written with and without a pasted scheme is one
    # registry; read as two, the fallback token would be dropped and cleanup
    # (and the tag GC) would silently stop for that region.
    settings = _settings(
        registry={"url": "https://registry.internal/", "api_token": "tok-platform"},
        regions=[
            RegionConfig(name="central", cluster="central-0"),
            RegionConfig(
                name="south",
                cluster="south-0",
                registry=RegionRegistry(url="registry.internal"),
            ),
        ],
    )
    assert settings.registry_for("south").api_token == "tok-platform"


def test_settings_carry_the_other_registry_fields_through():
    settings = _settings(
        registry={"url": "registry.internal", "delete_on_function_delete": False, "timeout": 3.0},
        regions=[
            RegionConfig(
                name="central",
                cluster="central-0",
                registry=RegionRegistry(url="registry.central.internal"),
            )
        ],
        region_registry_tokens={"central": "tok"},
    )
    reg = settings.registry_for("central")
    assert reg.timeout == 3.0
    assert reg.delete_on_function_delete is False
    assert not reg.can_delete  # credentialed, but switched off


def test_a_cluster_carries_its_own_regions_registry():
    """So a caller holding a cluster cannot reach for the platform default.

    That default is a real attribute that silently means the wrong registry on
    any per-region path, which is the mistake this removes the opportunity for.
    """
    from common.cluster import clusters_for

    settings = _settings(
        regions=[
            RegionConfig(
                name="central",
                cluster="central-0",
                registry=RegionRegistry(url="registry.central.internal"),
            ),
            RegionConfig(name="south", cluster="south-0"),
        ],
        region_registry_tokens={"central": "tok-central"},
    )
    clusters = clusters_for(settings)

    assert clusters["central"].registry.base == "registry.central.internal/acme/serverless"
    assert clusters["central"].registry.api_token == "tok-central"
    # the region with no override still resolves to the platform default
    assert clusters["south"].registry.base == "registry.internal/acme/serverless"


def test_regions_and_tokens_load_from_the_environment(monkeypatch):
    # The shape the chart renders: regions as JSON in a ConfigMap, tokens as a
    # region-keyed JSON object from the ExternalSecret.
    monkeypatch.setenv(
        "SERVERLESS_REGIONS",
        '[{"name":"central","cluster":"central-0",'
        '"registry":{"url":"registry.central.internal"}},'
        '{"name":"south","cluster":"south-0"}]',
    )
    monkeypatch.setenv("SERVERLESS_REGION_REGISTRY_TOKENS", '{"central":"tok-central"}')
    monkeypatch.setenv("SERVERLESS_REGISTRY__URL", "registry.internal")
    settings = CommonSettings()
    assert settings.region_names == ["central", "south"]
    assert settings.registry_for("central").url == "registry.central.internal"
    assert settings.registry_for("central").api_token == "tok-central"
    assert settings.registry_for("south").url == "registry.internal"
