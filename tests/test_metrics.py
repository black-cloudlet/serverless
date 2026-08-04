from api.services.metrics import (
    parse_cpu_millicores,
    parse_memory_bytes,
    pod_usage,
    sum_usage,
)


def test_parse_quantities():
    assert parse_cpu_millicores("250m") == 250
    assert parse_cpu_millicores("1") == 1000  # 1 core = 1000m
    assert round(parse_cpu_millicores("1500000n")) == 2  # nanocores -> ~1.5m
    assert parse_memory_bytes("256Mi") == 256 * 2**20
    assert parse_memory_bytes("123456Ki") == 123456 * 2**10


def test_sum_usage_aggregates_over_pods_and_containers():
    items = [
        {"containers": [{"usage": {"cpu": "100m", "memory": "128Mi"}}]},
        {
            "containers": [
                {"usage": {"cpu": "150m", "memory": "256Mi"}},
                {"usage": {"cpu": "50m", "memory": "0"}},
            ]
        },
    ]
    u = sum_usage(items)
    assert u.cpu == "300m"
    assert u.memory == "384Mi"


def test_sum_usage_empty_is_none():
    assert sum_usage([]) is None
    assert sum_usage([{"containers": []}]) is None


def _pod(name, revision, containers):
    return {
        "metadata": {"name": name, "labels": {"serving.knative.dev/revision": revision}},
        "containers": containers,
    }


def test_pod_usage_breaks_the_total_down_per_replica():
    items = [
        _pod("fn-team-00002-a", "fn-team-00002", [{"usage": {"cpu": "100m", "memory": "128Mi"}}]),
        _pod(
            "fn-team-00002-b",
            "fn-team-00002",
            [
                {"usage": {"cpu": "150m", "memory": "256Mi"}},
                # the injected sidecar is excluded here exactly as it is in the sum
                {"name": "queue-proxy", "usage": {"cpu": "900m", "memory": "900Mi"}},
            ],
        ),
    ]
    pods = pod_usage(items)
    assert [p.pod for p in pods] == ["fn-team-00002-a", "fn-team-00002-b"]
    assert [p.revision for p in pods] == ["fn-team-00002", "fn-team-00002"]
    assert (pods[0].usage.cpu, pods[0].usage.memory) == ("100m", "128Mi")
    assert (pods[1].usage.cpu, pods[1].usage.memory) == ("150m", "256Mi")


def test_pod_usage_and_sum_usage_agree():
    # Quantities that do not round cleanly: the total must be the sum of the raw
    # figures, not of the rounded per-pod ones, or the two views disagree.
    items = [
        _pod("a", "r", [{"usage": {"cpu": "500u", "memory": "1536Ki"}}]),
        _pod("b", "r", [{"usage": {"cpu": "500u", "memory": "1536Ki"}}]),
    ]
    pods = pod_usage(items)
    assert [p.usage.cpu for p in pods] == ["0m", "0m"]  # 0.5m each, rounds to banker's 0
    assert sum_usage(items).cpu == "1m"  # but the site total is 1m, not 0m
    assert sum_usage(items).memory == "3Mi"


def test_pod_usage_omits_pods_that_reported_nothing():
    # A pod the metrics-server has not scraped yet is absent, not a zero row.
    assert pod_usage([_pod("fresh", "r", [])]) == []
    assert pod_usage([]) == []


def test_pod_usage_tolerates_a_pod_without_metadata():
    pods = pod_usage([{"containers": [{"usage": {"cpu": "10m", "memory": "10Mi"}}]}])
    assert len(pods) == 1
    assert pods[0].pod == ""
    assert pods[0].revision is None
