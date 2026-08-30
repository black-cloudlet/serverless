from api.services.state.metrics import (
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


def test_total_sums_regions_before_rounding():
    # Two regions of 0.5m each: rounded first they are "0m" + "0m", summed raw the
    # workload is 1m. Rounding at the edge is what keeps the total honest.
    region = total_usage([{"containers": [{"usage": {"cpu": "500u", "memory": "1536Ki"}}]}])
    assert region.quantities().cpu == "0m"
    assert total([region, region]).quantities().cpu == "1m"
    assert total([region, region]).quantities().memory == "3Mi"


def test_usage_adds_componentwise():
    assert Usage(10.0, 100.0) + Usage(5.0, 50.0) == Usage(15.0, 150.0)


def test_an_unparseable_quantity_never_escapes_the_usage_read():
    """A region whose usage cannot be parsed reports unmeasured, not Failed.

    ``region_usage`` promises never to raise, and the promise is load-bearing: it
    runs inside the ``/stats`` fan-out, so an escaping ValueError becomes a
    ``Failed`` region and a ``Failed`` rollup for a workload that is serving.
    Kubernetes may render a quantity in a form the parser does not know (e.g.
    decimal-exponent notation), which is exactly when this matters.
    """
    from api.services.regions.region_read import region_usage

    class Cluster:
        region = "central"

        def get(self, kind, name=None, label_selector=None, namespace=None, field_selector=None):
            return [{"containers": [{"usage": {"cpu": "1e3n", "memory": "1e6"}}]}]

    read = region_usage(Cluster(), "orders-api-team")
    assert read.measured is False
    assert read.total is None


def test_a_readable_region_still_reports_its_usage():
    from api.services.regions.region_read import region_usage

    class Cluster:
        region = "central"

        def get(self, kind, name=None, label_selector=None, namespace=None, field_selector=None):
            return [{"containers": [{"usage": {"cpu": "120m", "memory": "180Mi"}}]}]

    read = region_usage(Cluster(), "orders-api-team")
    assert read.measured is True
    assert read.total.quantities().cpu == "120m"
