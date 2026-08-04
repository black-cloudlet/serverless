from api.services.metrics import (
    Usage,
    parse_cpu_millicores,
    parse_memory_bytes,
    total,
    total_usage,
)


def quantities(items):
    measured = total_usage(items)
    return measured.quantities() if measured else None


def test_parse_quantities():
    assert parse_cpu_millicores("250m") == 250
    assert parse_cpu_millicores("1") == 1000  # 1 core = 1000m
    assert round(parse_cpu_millicores("1500000n")) == 2  # nanocores -> ~1.5m
    assert parse_memory_bytes("256Mi") == 256 * 2**20
    assert parse_memory_bytes("123456Ki") == 123456 * 2**10


def test_total_usage_aggregates_over_pods_and_containers():
    items = [
        {"containers": [{"usage": {"cpu": "100m", "memory": "128Mi"}}]},
        {
            "containers": [
                {"usage": {"cpu": "150m", "memory": "256Mi"}},
                {"usage": {"cpu": "50m", "memory": "0"}},
            ]
        },
    ]
    u = quantities(items)
    assert u.cpu == "300m"
    assert u.memory == "384Mi"


def test_total_usage_excludes_the_queue_proxy_sidecar():
    items = [
        {
            "containers": [
                {"name": "user-container", "usage": {"cpu": "60m", "memory": "90Mi"}},
                {"name": "queue-proxy", "usage": {"cpu": "999m", "memory": "999Mi"}},
            ]
        }
    ]
    assert quantities(items).cpu == "60m"


def test_nothing_measured_is_none_not_zero():
    # "consuming nothing" and "nobody measured" must not render as the same answer
    assert total_usage([]) is None
    assert total_usage([{"containers": []}]) is None
    assert total([]) is None


def test_total_sums_sites_before_rounding():
    # Two sites of 0.5m each: rounded first they are "0m" + "0m", summed raw the
    # workload is 1m. Rounding at the edge is what keeps the total honest.
    site = total_usage([{"containers": [{"usage": {"cpu": "500u", "memory": "1536Ki"}}]}])
    assert site.quantities().cpu == "0m"
    assert total([site, site]).quantities().cpu == "1m"
    assert total([site, site]).quantities().memory == "3Mi"


def test_usage_adds_componentwise():
    assert Usage(10.0, 100.0) + Usage(5.0, 50.0) == Usage(15.0, 150.0)
