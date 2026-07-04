from app.services.metrics import parse_cpu_millicores, parse_memory_bytes, sum_usage


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
