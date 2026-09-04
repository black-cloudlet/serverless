"""Tenant controller core: the template set, the converge protocol, and the loop.

What matters here is the crash-safety contract (the hash is stamped last, so
any interrupted converge redoes itself), the prune staying inside the
tenant controller's own labels, and an empty template set being refused rather than
obeyed - each is a way tenant state could otherwise be lost silently.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import loop as common_loop
from common.cluster import ResourceKind
from common.errors import NotFoundError
from common.labels import (
    ANNOTATION_TEMPLATE_HASH,
    LABEL_GROUP,
    LABEL_MANAGED_BY,
    MANAGED_BY_VALUE,
    TENANT_CONTROLLER_VALUE,
)
from tenant_controller import main as controller_main
from tenant_controller import reconcile as controller_reconcile
from tenant_controller.config import TenantControllerSettings
from tenant_controller.reconcile import (
    FIELD_MANAGER,
    TENANT_CONTROLLER_SELECTOR,
    converge,
    reconcile_all,
)
from tenant_controller.templates import TemplateSet

NS_TEMPLATE = """\
apiVersion: v1
kind: Namespace
metadata:
  name: "{{namespace}}"
  labels:
    team: platform
"""

POLICY_TEMPLATE = """\
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
  namespace: "{{namespace}}"
spec:
  podSelector: {}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: ca-bundle
  namespace: "{{namespace}}"
"""


def _set(files: dict[str, str] | None = None) -> TemplateSet:
    files = (
        {"10-namespace.yaml": NS_TEMPLATE, "20-policies.yaml": POLICY_TEMPLATE}
        if files is None
        else files
    )
    return TemplateSet.from_sources(files.items())


class _Registry:
    url = "registry.central.internal"


class _Cluster:
    """A cluster-scoped fake: canned namespaces and labeled objects, recording writes."""

    region = "central"
    name = "central-0"
    registry = _Registry()

    def __init__(self, namespaces=None, objects=None, fail_apply_at=None, unserved=()):
        self._namespaces = namespaces or []
        # {(kind, namespace): [obj]} - what a labeled list returns per kind.
        self._objects = objects or {}
        # Kinds this cluster's apiserver does not have (an optional CRD).
        self._unserved = frozenset(unserved)
        self.applied = []  # (manifest, namespace, field_manager)
        self.deleted = []  # (kind, name, namespace)
        self.lists = []  # (kind, namespace) per get, to count LIST round trips
        self._fail_apply_at = fail_apply_at
        self._applies = 0

    def serves(self, kind):
        return kind not in self._unserved

    def get(self, kind, name=None, label_selector=None, *, namespace):
        assert label_selector == TENANT_CONTROLLER_SELECTOR
        assert self.serves(kind), f"{kind.kind} is not served by this cluster"
        self.lists.append((kind, namespace))
        if kind is ResourceKind.NAMESPACE:
            assert namespace is None, "namespaces are cluster-scoped"
            return list(self._namespaces)
        assert namespace is not None, "objects are listed per namespace, never cluster-wide"
        return list(self._objects.get((kind, namespace), []))

    def apply(self, manifest, *, namespace, field_manager=None):
        self._applies += 1
        if self._fail_apply_at is not None and self._applies >= self._fail_apply_at:
            raise RuntimeError("apply refused")
        self.applied.append((manifest, namespace, field_manager))
        return [manifest]

    def delete(self, kind, name, *, namespace):
        self.deleted.append((kind, name, namespace))


def _leftover(name="stale-policy", kind_str="NetworkPolicy"):
    return {
        "kind": kind_str,
        "metadata": {"name": name, "labels": {LABEL_MANAGED_BY: TENANT_CONTROLLER_VALUE}},
    }


# --------------------------------------------------------------------------- #
# TemplateSet                                                                   #
# --------------------------------------------------------------------------- #


def test_load_reads_only_visible_files_and_hashes_stably(tmp_path):
    # A mounted ConfigMap directory holds the kubelet's ..data machinery
    # beside the keys; only the keys are templates.
    (tmp_path / "b.yaml").write_text(POLICY_TEMPLATE)
    (tmp_path / "a.yaml").write_text(NS_TEMPLATE)
    (tmp_path / "..data").mkdir()
    (tmp_path / ".hidden").write_text("not: a-template")

    loaded = TemplateSet.load(tmp_path)

    assert [name for name, _ in loaded.sources] == ["a.yaml", "b.yaml"]
    # The digest is a property of the set's content, not the listing order.
    assert loaded.digest == _set({"a.yaml": NS_TEMPLATE, "b.yaml": POLICY_TEMPLATE}).digest


def test_the_digest_names_the_set_not_the_group():
    # Every namespace converged from one ConfigMap carries one stamp: the
    # hash is over the raw text, before any substitution.
    templates = _set()
    a = templates.render(
        namespace="a-serverless", group="a", region="central", registry="registry.central.internal"
    )
    b = templates.render(
        namespace="b-serverless", group="b", region="central", registry="registry.central.internal"
    )
    assert a != b
    assert templates.digest == _set().digest


def test_render_substitutes_both_placeholders():
    manifests = _set().render(
        namespace="payments-serverless",
        group="payments",
        region="central",
        registry="registry.central.internal",
    )
    ns = next(m for m in manifests if m["kind"] == "Namespace")
    assert ns["metadata"]["name"] == "payments-serverless"
    policy = next(m for m in manifests if m["kind"] == "NetworkPolicy")
    assert policy["metadata"]["namespace"] == "payments-serverless"


def test_an_unquoted_placeholder_is_legal_yaml():
    # YAML authors write `name: {{namespace}}` unquoted; parsed as-is that is
    # a flow mapping with an unhashable key, so placeholders become sentinels
    # BEFORE the parse.
    templates = _set(
        {
            "t.yaml": (
                "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
                "  name: {{namespace}}-ca\n  namespace: {{namespace}}\n"
                "data:\n  group: {{group}}\n"
            )
        }
    )

    [cm] = templates.render(
        namespace="payments-serverless",
        group="payments",
        region="central",
        registry="registry.central.internal",
    )

    assert cm["metadata"]["name"] == "payments-serverless-ca"
    assert cm["metadata"]["namespace"] == "payments-serverless"
    assert cm["data"]["group"] == "payments"


def test_an_unknown_placeholder_fails_at_load_with_the_file_named():
    # A bad set fails before any namespace is touched, like a bad kind.
    with pytest.raises(ValueError, match="bad.yaml"):
        _set({"bad.yaml": "kind: ConfigMap\nmetadata:\n  name: '{{namespce}}'\n"})


def test_an_unknown_placeholder_in_a_comment_is_caught_too():
    # The check runs on the raw text, so a typo an author left in a
    # commented-out block still fails loudly rather than shipping silently.
    with pytest.raises(ValueError, match="namepace"):
        _set(
            {
                "bad.yaml": "# todo: restore for {{namepace}}\nkind: ConfigMap\nmetadata:\n  name: x\n"
            }
        )


def test_yaml_that_does_not_parse_names_its_file_as_a_ValueError():
    # An operator reading the backoff log needs the ConfigMap key to fix;
    # a bare ScannerError names only "<unicode string>, line 3".
    with pytest.raises(ValueError, match="bad.yaml.*not valid YAML"):
        _set({"bad.yaml": "kind: ConfigMap\n\tbad: indent\n"})


@pytest.mark.parametrize(
    "text",
    [
        "- just\n- a\n- list\n",
        "metadata:\n  name: no-kind\n",
        "kind: ConfigMap\nmetadata: {}\n",
        "kind: NotAThing\nmetadata:\n  name: x\n",
    ],
)
def test_a_malformed_manifest_is_rejected(text):
    with pytest.raises(ValueError):
        _set({"t.yaml": text})


# --------------------------------------------------------------------------- #
# converge: the stamp protocol                                                  #
# --------------------------------------------------------------------------- #


def test_converge_orders_namespace_contents_stamp():
    cluster = _Cluster()

    converge(cluster, "payments-serverless", "payments", _set())

    kinds = [m["kind"] for m, _ns, _fm in cluster.applied]
    # Namespace opens and closes the converge; contents sit between.
    assert kinds[0] == "Namespace" and kinds[-1] == "Namespace"
    assert set(kinds[1:-1]) == {"NetworkPolicy", "ConfigMap"}
    # Cluster-scoped vs namespaced: the Namespace applies carry no namespace,
    # the contents carry the tenant's.
    assert [ns for m, ns, _fm in cluster.applied if m["kind"] == "Namespace"] == [None, None]
    assert {ns for m, ns, _fm in cluster.applied if m["kind"] != "Namespace"} == {
        "payments-serverless"
    }
    # Every write is the tenant controller's own SSA identity.
    assert {fm for _m, _ns, fm in cluster.applied} == {FIELD_MANAGER}


def test_the_hash_is_stamped_last_and_cleared_first():
    cluster = _Cluster()
    templates = _set()

    converge(cluster, "payments-serverless", "payments", templates)

    first, last = cluster.applied[0][0], cluster.applied[-1][0]
    # The opening apply declares no hash - under SSA that *removes* the old
    # stamp, marking the converge in progress...
    assert ANNOTATION_TEMPLATE_HASH not in first["metadata"].get("annotations", {})
    # ...and only the closing apply writes the new one.
    assert last["metadata"]["annotations"][ANNOTATION_TEMPLATE_HASH] == templates.digest


def test_a_crashed_converge_leaves_no_stamp_so_the_next_pass_redoes_it():
    templates = _set()
    # Fail after the namespace and the first content apply - mid-converge.
    cluster = _Cluster(fail_apply_at=3)

    with pytest.raises(RuntimeError):
        converge(cluster, "payments-serverless", "payments", templates)

    stamped = [
        m
        for m, _ns, _fm in cluster.applied
        if ANNOTATION_TEMPLATE_HASH in (m["metadata"].get("annotations") or {})
    ]
    assert stamped == []  # nothing recorded the new hash, so staleness persists


def test_converge_injects_the_ownership_labels_everywhere():
    # Injected in code, not trusted to the templates: the prune and the GC
    # select on these labels.
    cluster = _Cluster()

    converge(cluster, "payments-serverless", "payments", _set())

    for manifest, _ns, _fm in cluster.applied:
        labels = manifest["metadata"]["labels"]
        assert labels[LABEL_MANAGED_BY] == TENANT_CONTROLLER_VALUE
        assert labels[LABEL_GROUP] == "payments"
    # The template's own labels survive beside them.
    ns = cluster.applied[0][0]
    assert ns["metadata"]["labels"]["team"] == "platform"


def test_converge_targets_the_found_namespace_whatever_the_template_says():
    # The loop converges the namespace it *found*: a changed suffix setting
    # must not strand existing namespaces under their old names, and a
    # template hardcoding a name must not create a second namespace.
    cluster = _Cluster()
    templates = _set(
        {
            "ns.yaml": "kind: Namespace\napiVersion: v1\nmetadata:\n  name: wrong\n",
            "p.yaml": POLICY_TEMPLATE,
        }
    )

    converge(cluster, "payments-serverless", "payments", templates)

    assert cluster.applied[0][0]["metadata"]["name"] == "payments-serverless"


def test_a_set_without_a_namespace_template_synthesizes_one():
    cluster = _Cluster()
    templates = _set({"p.yaml": POLICY_TEMPLATE})

    converge(cluster, "payments-serverless", "payments", templates)

    assert cluster.applied[0][0]["kind"] == "Namespace"
    assert cluster.applied[0][0]["metadata"]["name"] == "payments-serverless"


# --------------------------------------------------------------------------- #
# prune                                                                         #
# --------------------------------------------------------------------------- #


def test_prune_deletes_a_managed_leftover_the_set_no_longer_renders():
    leftover = _leftover("dropped-policy")
    cluster = _Cluster(objects={(ResourceKind.NETWORK_POLICY, "payments-serverless"): [leftover]})

    converge(cluster, "payments-serverless", "payments", _set())

    assert (ResourceKind.NETWORK_POLICY, "dropped-policy", "payments-serverless") in (
        cluster.deleted
    )


def test_prune_keeps_what_the_set_still_renders():
    rendered = _leftover("default-deny")  # same name the template renders
    cluster = _Cluster(objects={(ResourceKind.NETWORK_POLICY, "payments-serverless"): [rendered]})

    converge(cluster, "payments-serverless", "payments", _set())

    assert cluster.deleted == []


def test_prune_sweeps_kinds_the_set_dropped_entirely():
    # PRUNABLE_KINDS is fixed rather than derived from the current set: a
    # kind removed from the set entirely still has leftovers to collect.
    leftover = _leftover("old-binding", "RoleBinding")
    cluster = _Cluster(objects={(ResourceKind.ROLE_BINDING, "payments-serverless"): [leftover]})

    converge(cluster, "payments-serverless", "payments", _set())

    assert (ResourceKind.ROLE_BINDING, "old-binding", "payments-serverless") in cluster.deleted


def test_prune_skips_a_kind_the_cluster_does_not_serve():
    # Part of the vocabulary is an optional add-on: Trident Protect's
    # Application and Schedule exist only where it is installed. Listing an
    # uninstalled CRD is a 404 on the resource itself, not an empty list, so a
    # prune that asked would fail every converge on every cluster without it -
    # including the ones that never enabled backups. The fake asserts the
    # question is not asked; the converge finishing is the point.
    cluster = _Cluster(
        unserved=(ResourceKind.TRIDENT_APPLICATION, ResourceKind.TRIDENT_SCHEDULE),
    )

    converge(cluster, "payments-serverless", "payments", _set())

    swept = {kind for kind, _namespace in cluster.lists}
    assert ResourceKind.TRIDENT_APPLICATION not in swept
    assert ResourceKind.TRIDENT_SCHEDULE not in swept
    # The kinds it does serve are still swept - the skip is per kind, not a
    # prune that gives up on the first miss.
    assert ResourceKind.NETWORK_POLICY in swept


def test_prune_collects_backups_left_behind_when_the_set_drops_them():
    # Trident Protect installed, `backup.enabled` turned off: the part leaves
    # the set, and the Schedules it applied are the tenant controller's to
    # collect. Nothing else would - they carry no owner reference, and the
    # namespace outlives the switch.
    leftover = _leftover("payments-serverless-central-hourly", "Schedule")
    cluster = _Cluster(objects={(ResourceKind.TRIDENT_SCHEDULE, "payments-serverless"): [leftover]})

    converge(cluster, "payments-serverless", "payments", _set())

    assert (
        ResourceKind.TRIDENT_SCHEDULE,
        "payments-serverless-central-hourly",
        "payments-serverless",
    ) in cluster.deleted


# --------------------------------------------------------------------------- #
# reconcile_all                                                                 #
# --------------------------------------------------------------------------- #


def _namespace(name, group, stamp=None):
    meta = {
        "name": name,
        "labels": {LABEL_MANAGED_BY: TENANT_CONTROLLER_VALUE, LABEL_GROUP: group},
    }
    if stamp:
        meta["annotations"] = {ANNOTATION_TEMPLATE_HASH: stamp}
    return {"kind": "Namespace", "metadata": meta}


def test_only_stale_namespaces_are_converged():
    templates = _set()
    cluster = _Cluster(
        namespaces=[
            _namespace("fresh-serverless", "fresh", stamp=templates.digest),
            _namespace("stale-serverless", "stale", stamp="0" * 16),
            _namespace("new-serverless", "new"),  # no stamp: never converged
        ]
    )

    seen, converged, failed = reconcile_all(cluster, templates)

    assert (seen, converged, failed) == (3, 2, 0)
    touched = {m["metadata"]["name"] for m, _ns, _fm in cluster.applied if m["kind"] == "Namespace"}
    assert touched == {"stale-serverless", "new-serverless"}


def test_one_failing_namespace_does_not_starve_the_rest():
    templates = _set()
    cluster = _Cluster(
        namespaces=[
            _namespace("broken-serverless", "broken"),
            _namespace("fine-serverless", "fine"),
        ]
    )

    # Fail every apply for the first namespace only.
    original_apply = cluster.apply

    def flaky(manifest, *, namespace, field_manager=None):
        if manifest["metadata"].get("labels", {}).get(LABEL_GROUP) == "broken":
            raise RuntimeError("region hiccup")
        return original_apply(manifest, namespace=namespace, field_manager=field_manager)

    cluster.apply = flaky

    seen, converged, failed = reconcile_all(cluster, templates)

    assert (seen, converged, failed) == (2, 1, 1)
    assert any(m["metadata"]["name"] == "fine-serverless" for m, _ns, _fm in cluster.applied)


def test_a_managed_namespace_without_a_group_is_skipped_loudly(caplog):
    cluster = _Cluster(
        namespaces=[{"kind": "Namespace", "metadata": {"name": "odd", "labels": {}}}]
    )

    seen, converged, failed = reconcile_all(cluster, _set())

    assert (seen, converged, failed) == (1, 0, 1)
    assert cluster.applied == []
    assert "no group label" in caplog.text


def test_an_empty_template_set_is_refused(caplog):
    # Mounted-but-empty is indistinguishable from a broken mount mid-update;
    # obeying it would prune every managed object out of every namespace.
    cluster = _Cluster(namespaces=[_namespace("a-serverless", "a")])

    assert reconcile_all(cluster, _set({})) == (0, 0, 0)
    assert cluster.applied == []
    assert cluster.deleted == []
    assert "refusing" in caplog.text


def test_the_api_stamp_is_not_the_controllers():
    # The selector is the fake's contract (it asserts on it), and the values
    # differ by construction - pinned so a refactor cannot quietly unify the
    # API's managed-by with the tenant controller's and let the prune eat workloads.
    assert TENANT_CONTROLLER_VALUE != MANAGED_BY_VALUE
    assert TENANT_CONTROLLER_SELECTOR.endswith(TENANT_CONTROLLER_VALUE)


# --------------------------------------------------------------------------- #
# the loop and the pass                                                         #
# --------------------------------------------------------------------------- #


def test_run_pass_loads_the_mounted_set_fresh_each_time(tmp_path, monkeypatch):
    (tmp_path / "ns.yaml").write_text(NS_TEMPLATE)
    settings = TenantControllerSettings(regions=[], templates_dir=str(tmp_path))
    seen = []
    monkeypatch.setattr(
        controller_main,
        "reconcile_all",
        lambda cluster, templates, **kw: (seen.append(templates.digest), (1, 1, 0))[1],
    )

    controller_main.run_pass(object(), settings)
    (tmp_path / "extra.yaml").write_text(POLICY_TEMPLATE)
    controller_main.run_pass(object(), settings)

    # The re-read is the hop that carries a helm upgrade to existing
    # namespaces: the second pass sees the changed set.
    assert len(seen) == 2 and seen[0] != seen[1]


class _NoopGC:
    def maybe_sweep(self):
        pass


def test_a_raising_pass_backs_off_and_a_clean_pass_waits_the_interval(monkeypatch):
    settings = TenantControllerSettings(regions=[], resync_seconds=300, error_backoff_seconds=5.0)
    outcomes = iter([RuntimeError("mount vanished"), None, SystemExit(0)])
    sleeps = []

    def fake_pass(cluster, s, *, force=False):
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(controller_main, "run_pass", fake_pass)
    monkeypatch.setattr(common_loop.time, "sleep", sleeps.append)

    with pytest.raises(SystemExit):
        controller_main.loop(object(), settings, _NoopGC())

    assert sleeps[0] == 5.0  # the raise took the backoff...
    assert sleeps[1] == pytest.approx(300, abs=2)  # ...the clean pass, the interval


def test_missing_templates_dir_raises_into_the_loops_backoff(tmp_path):
    settings = TenantControllerSettings(regions=[], templates_dir=str(tmp_path / "absent"))
    with pytest.raises(FileNotFoundError):
        controller_main.run_pass(object(), settings)


def test_settings_defaults():
    s = TenantControllerSettings(regions=[])
    assert s.resync_seconds == 300
    assert s.templates_dir == "/etc/serverless/tenant-templates"
    assert s.full_resync_passes == 12
    assert s.converge_workers == 4


def test_a_set_that_renders_nothing_refuses_to_converge():
    # Files that render to zero manifests read as a truncated ConfigMap;
    # obeying them would prune the namespace bare and stamp it converged.
    templates = _set({"20-policies.yaml": "# rendered empty by a values flag\n"})
    leftover = _leftover("default-deny")
    cluster = _Cluster(
        namespaces=[_namespace("x-serverless", "x", stamp="old")],
        objects={(ResourceKind.NETWORK_POLICY, "x-serverless"): [leftover]},
    )

    seen, converged, failed = reconcile_all(cluster, templates)

    assert (seen, converged, failed) == (1, 0, 1)
    assert cluster.deleted == []  # nothing pruned...
    assert cluster.applied == []  # ...and the stamp untouched, so it retries


def test_a_kind_outside_the_vocabulary_is_rejected_at_load():
    # What a set admits, the prune must be able to collect: 'Service' is not
    # in the vocabulary, so it cannot become an uncollectable leftover. The
    # refusal is at construction - a bad set fails into the loop's backoff
    # before any namespace is touched.
    with pytest.raises(ValueError, match="does not manage"):
        _set({"t.yaml": "apiVersion: v1\nkind: Service\nmetadata:\n  name: x\n"})


def test_payload_braces_are_not_placeholders():
    # A tenant-facing ConfigMap may legitimately carry a Go-template payload;
    # only bare lowercase {{token}} shapes are placeholders.
    templates = _set(
        {
            "cm.yaml": (
                "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: dash\n"
                "data:\n  tmpl: '{{ .Values.x }}'\n"
            )
        }
    )
    [cm] = templates.render(
        namespace="n", group="g", region="central", registry="registry.central.internal"
    )
    assert cm["data"]["tmpl"] == "{{ .Values.x }}"


def test_contents_namespace_is_forced_to_the_target():
    # A hardcoded namespace in a content template must not decide where the
    # object lands - the target wins, as its name does for the Namespace.
    templates = _set(
        {
            "cm.yaml": (
                "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: ca\n"
                "  namespace: somewhere-else\n"
            )
        }
    )
    cluster = _Cluster()

    converge(cluster, "payments-serverless", "payments", templates)

    cm = next(m for m, _ns, _fm in cluster.applied if m["kind"] == "ConfigMap")
    assert cm["metadata"]["namespace"] == "payments-serverless"


def test_force_reconverges_a_matching_stamp():
    # The periodic drift repair: a deleted object does not change the stamp,
    # so every Nth pass converges even fresh-looking namespaces.
    templates = _set()
    cluster = _Cluster(namespaces=[_namespace("a-serverless", "a", stamp=templates.digest)])

    assert reconcile_all(cluster, templates) == (1, 0, 0)
    assert cluster.applied == []
    assert reconcile_all(cluster, templates, force=True) == (1, 1, 0)
    assert cluster.applied != []


def test_an_all_failed_pass_raises_into_the_backoff(tmp_path, monkeypatch):
    # Every namespace failing is one cause, not many: the pass fails, so the
    # loop backs off instead of sleeping a full resync on it.
    (tmp_path / "ns.yaml").write_text(NS_TEMPLATE)
    settings = TenantControllerSettings(regions=[], templates_dir=str(tmp_path))
    monkeypatch.setattr(
        controller_main, "reconcile_all", lambda cluster, templates, **kw: (3, 0, 3)
    )
    with pytest.raises(RuntimeError, match="all 3"):
        controller_main.run_pass(object(), settings)

    # A partial failure is namespaces' business, not the pass's.
    monkeypatch.setattr(
        controller_main, "reconcile_all", lambda cluster, templates, **kw: (3, 2, 1)
    )
    controller_main.run_pass(object(), settings)


def test_objects_are_listed_per_namespace_never_cluster_wide():
    # A tenant controller that could list every Secret in the cluster is a far
    # larger grant than one scoped to the namespaces it owns; only the
    # Namespace listing is cluster-scoped. (The fake asserts this too.)
    templates = _set()
    cluster = _Cluster(namespaces=[_namespace("a-serverless", "a")])

    reconcile_all(cluster, templates)

    scopes = {ns for kind, ns in cluster.lists if kind is not ResourceKind.NAMESPACE}
    assert scopes == {"a-serverless"}


def test_a_leftover_is_pruned_from_its_own_namespace_only():
    templates = _set()
    cluster = _Cluster(
        namespaces=[
            _namespace("a-serverless", "a"),
            _namespace("b-serverless", "b"),
        ],
        objects={(ResourceKind.NETWORK_POLICY, "a-serverless"): [_leftover("dropped")]},
    )

    reconcile_all(cluster, templates)

    assert cluster.deleted == [(ResourceKind.NETWORK_POLICY, "dropped", "a-serverless")]


def test_every_converge_opens_by_clearing_the_stamp():
    # The protocol has one shape: the opening apply is also the write that
    # creates the namespace, so no caller can skip it on a stale read of a
    # stamp someone else wrote.
    templates = _set()
    cluster = _Cluster(
        namespaces=[
            _namespace("new-serverless", "new"),  # never converged
            _namespace("stale-serverless", "stale", stamp="0" * 16),
        ]
    )

    reconcile_all(cluster, templates)

    ns_applies: dict[str, int] = {}
    for m, _ns, _fm in cluster.applied:
        if m["kind"] == "Namespace":
            ns_applies[m["metadata"]["name"]] = ns_applies.get(m["metadata"]["name"], 0) + 1
    assert ns_applies == {"new-serverless": 2, "stale-serverless": 2}


def test_workers_converge_all_namespaces():
    # Converges are independent per namespace (the stamp is per namespace),
    # so a pool changes wall time, never the outcome.
    templates = _set()
    cluster = _Cluster(namespaces=[_namespace(f"g{i}-serverless", f"g{i}") for i in range(5)])

    assert reconcile_all(cluster, templates, workers=3) == (5, 5, 0)
    stamped = {
        m["metadata"]["name"]
        for m, _ns, _fm in cluster.applied
        if m["kind"] == "Namespace"
        and ANNOTATION_TEMPLATE_HASH in (m["metadata"].get("annotations") or {})
    }
    assert stamped == {f"g{i}-serverless" for i in range(5)}


def test_a_signal_mid_pass_drops_the_queued_converges(monkeypatch):
    """map() submits every stale namespace up front, so the pass must drop the queue.

    Asserted as the call we make, not as a count of how far a worker got:
    what ``cancel_futures`` then does is CPython's to guarantee, and racing a
    thread against the interpreter to observe it only buys a flaky test.
    """
    templates = _set()
    cluster = _Cluster(namespaces=[_namespace(f"g{i}-serverless", f"g{i}") for i in range(40)])
    shutdowns = []

    class _RecordingPool(ThreadPoolExecutor):
        def shutdown(self, wait=True, *, cancel_futures=False):
            shutdowns.append({"wait": wait, "cancel_futures": cancel_futures})
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(controller_reconcile, "ThreadPoolExecutor", _RecordingPool)
    real_apply = cluster.apply
    applied = []

    def apply(manifest, *, namespace, field_manager=None):
        if manifest["kind"] == "Namespace":
            applied.append(manifest["metadata"]["name"])
            if len(applied) == 3:
                raise SystemExit(0)  # the signal handler, mid-pass
        return real_apply(manifest, namespace=namespace, field_manager=field_manager)

    cluster.apply = apply

    with pytest.raises(SystemExit):
        reconcile_all(cluster, templates, workers=1)

    assert shutdowns == [{"wait": True, "cancel_futures": True}]


def test_deletes_tolerate_not_found():
    # A leftover deleted by the peer pass between list and delete is not an
    # error; converge must not fail the namespace over it.
    class _GoneCluster(_Cluster):
        def delete(self, kind, name, *, namespace):
            super().delete(kind, name, namespace=namespace)
            raise NotFoundError(name)

    leftover = _leftover("dropped")
    cluster = _GoneCluster(objects={(ResourceKind.NETWORK_POLICY, "p-serverless"): [leftover]})

    converge(cluster, "p-serverless", "p", _set())  # must not raise


class _Server:
    """Stands in for the uvicorn server the API thread runs."""

    should_exit = False


class _Thread:
    """Stands in for that thread, recording the join that waits on it."""

    def __init__(self, events):
        self._events = events

    def join(self, timeout=None):
        self._events.append("joined")

    def is_alive(self):
        return False  # it stopped when asked, so no "did not stop" warning


def test_run_gives_the_api_every_region_but_the_loop_only_the_local_one(monkeypatch):
    """The split the design turns on: provision fans out, the loop never does."""
    events = []
    served = {}
    looped = {}

    class _Cluster:
        def __init__(self, region):
            self.region = region

        def close(self):
            events.append(f"closed {self.region}")

    clusters = {"central": _Cluster("central"), "south": _Cluster("south")}
    monkeypatch.setattr(controller_main, "clusters_for", lambda s: clusters)
    monkeypatch.setattr(controller_main, "select_local", lambda c, local: c["central"])

    def _serve(settings, given):
        served["clusters"] = given
        served["server"] = _Server()
        events.append("served")
        return served["server"], _Thread(events)

    def _loop(cluster, settings, gc):
        looped["cluster"] = cluster
        raise SystemExit(0)

    monkeypatch.setattr(controller_main, "serve", _serve)
    monkeypatch.setattr(controller_main, "loop", _loop)

    with pytest.raises(SystemExit):
        controller_main.run()

    assert [c.region for c in served["clusters"]] == ["central", "south"]
    assert looped["cluster"].region == "central"
    # Shut down in order, even when the loop ends by signal: the API is asked
    # to stop before the clients an in-flight provision is still writing through.
    assert served["server"].should_exit is True
    assert events == ["served", "joined", "closed central", "closed south"]


def test_the_api_runs_on_a_daemon_thread_off_the_loop(monkeypatch):
    """Off the main thread, so uvicorn installs no handler over the loop's."""
    built = {}

    class _RecordingServer:
        def __init__(self, config):
            built["config"] = config
            self.should_exit = False
            self.started = False

        def run(self):
            built["ran"] = True
            self.started = True

    monkeypatch.setattr(controller_main.uvicorn, "Server", _RecordingServer)
    monkeypatch.setattr(controller_main, "create_app", lambda clusters, settings: "app")
    settings = TenantControllerSettings(port=9999)

    server, thread = controller_main.serve(settings, [])
    thread.join(timeout=5)

    assert thread.daemon and thread.name == "provision-api"
    assert built["ran"], "the thread actually served"
    assert built["config"].port == 9999
    # Ours is already configured; uvicorn's dictConfig would replace it.
    assert built["config"].log_config is None

    controller_main.stop(server, thread)
    assert server.should_exit is True


# pytest installs its own threading.excepthook and warns about the SystemExit
# this test raises on purpose; plain Python discards it, which is the point.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_server_that_never_binds_fails_the_pod_instead_of_hiding(monkeypatch):
    """uvicorn answers a bind failure with sys.exit *in the thread*, and Python
    discards that silently - so without this check the loop would run on beside
    a dead API and every provision would be refused with nothing in the log."""

    class _DyingServer:
        def __init__(self, config):
            self.started = False
            self.should_exit = False

        def run(self):
            raise SystemExit(3)  # what uvicorn does when the port is taken

    monkeypatch.setattr(controller_main.uvicorn, "Server", _DyingServer)
    monkeypatch.setattr(controller_main, "create_app", lambda clusters, settings: "app")

    with pytest.raises(RuntimeError, match="stopped before it began serving"):
        controller_main.serve(TenantControllerSettings(), [])


def test_a_server_that_never_comes_up_is_not_waited_on_forever(monkeypatch):
    class _SilentServer:
        started = False
        should_exit = False

        def __init__(self, config):
            pass

        def run(self):
            time.sleep(5)  # alive, but never listening

    monkeypatch.setattr(controller_main.uvicorn, "Server", _SilentServer)
    monkeypatch.setattr(controller_main, "create_app", lambda clusters, settings: "app")
    monkeypatch.setattr(controller_main, "API_STARTUP_SECONDS", 0.2)

    with pytest.raises(RuntimeError, match="did not start within"):
        controller_main.serve(TenantControllerSettings(), [])
