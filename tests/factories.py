"""Builders shared by the deployer, workload-service, kpack and routing test modules."""

from api.core.config import RegionConfig, Settings
from api.services.regions.deployer import Deployer
from tests.conftest import plan_for


def settings_with_regions():
    return Settings(
        regions=[
            RegionConfig(name="region-a", cluster="region-a-0"),
            RegionConfig(name="region-b", cluster="region-b-0"),
        ]
    )


def _list_matching(objects, kind, field_selector):
    """The apiserver's list semantics, shared by both fakes: the kind, then the
    ``metadata.name`` field selector. A fake that ignored either would hand the
    caller objects a real cluster never would - or withhold ones it would."""
    items = [o for o in objects if o.get("kind") in (None, kind.kind)]
    if field_selector and field_selector.startswith("metadata.name="):
        wanted = field_selector.split("=", 1)[1]
        items = [o for o in items if (o.get("metadata") or {}).get("name") == wanted]
    return items


class _FakeCluster:
    def __init__(self, name, existing=None):
        self.name = name
        self.region = name
        self._existing = existing or {}

    def get(self, kind, name=None, label_selector=None, namespace=None, field_selector=None):
        from common.errors import NotFoundError as _NF

        # No name is a LIST, and an empty cluster lists empty - it does not
        # 404. The host pre-flight lists cluster-wide now, so a fake that
        # raised here would read as an unreachable region.
        if name is None:
            return _list_matching(self._existing.values(), kind, field_selector)
        if name in self._existing:
            return self._existing[name]
        raise _NF(f"{name} not found")


class _NullBuilder:
    """Builder that declares nothing; for the non-function paths."""

    pull_secret = "reg-creds"

    def image_ref(self, req, registry=None):
        return "reg/built:1"

    def plan(self, req, labels, registries):

        return plan_for(registries, self.image_ref(req))

    def status(self, cluster, name, group):
        return None

    def statuses(self, cluster, group):
        return {}


def _workload_service(clusters, builder=None, local_region=None):
    from api.services.workloads import WorkloadService

    settings = settings_with_regions()
    d = Deployer(settings)
    d._clusters = clusters  # inject fakes (name -> _FakeCluster)
    d._local_region = local_region
    return WorkloadService(settings, d, builder or _NullBuilder())


class _ApplyCluster:
    """Records applied manifests; serves a preset existing KSVC (and Secrets)."""

    def __init__(self, name, existing, secrets=None, images=None, registry=None):
        from common.config import RegistryConfig

        self.region = name
        self.name = name
        # A real Cluster resolves its region's registry at construction; a fake
        # that stands in for one has to carry it too.
        self.registry = registry or RegistryConfig()
        self._existing = existing  # oname -> ksvc dict
        self._secrets = secrets or {}  # secret name -> secret dict (preset)
        self._images = images or {}  # kpack Image name -> object (preset)
        self.applied = []
        self.deleted = []  # [(ResourceKind, name)] from prune / re-tag

    def get(self, kind, name=None, label_selector=None, namespace=None, field_selector=None):
        from common.cluster import ResourceKind
        from common.errors import NotFoundError as _NF

        # A LIST serves what this cluster holds - preset and applied alike, so
        # the host pre-flight sees the DomainMappings earlier operations wrote.
        if name is None:
            return _list_matching([*self._existing.values(), *self.applied], kind, field_selector)
        if kind == ResourceKind.KNATIVE_SERVICE and name in self._existing:
            return self._existing[name]
        if kind == ResourceKind.SECRET and name in self._secrets:
            return self._secrets[name]
        if kind == ResourceKind.KPACK_IMAGE and name in self._images:
            return self._images[name]
        raise _NF("not found")  # domain mapping -> Available; missing ksvc/secret/image

    def apply(self, manifest, namespace=None):
        self.applied.append(manifest)
        # Mirror the real client: return the applied object(s), with a uid the
        # server would assign (used to build ownerReferences for derived objects).
        meta = manifest.get("metadata", {}) or {}
        applied = {**manifest, "metadata": {**meta, "uid": f"uid-{meta.get('name')}"}}
        if manifest.get("kind") == "Service":  # now readable back by get()
            self._existing[meta.get("name")] = applied
        return [applied]

    def delete(self, kind, name, namespace=None):
        self.deleted.append((kind, name))


def _applied_kind(cluster, kind):
    return [m for m in cluster.applied if m.get("kind") == kind]
