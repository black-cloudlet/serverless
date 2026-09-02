"""Extract the chart's tenant template set and render it for one group.

The seam this closes: the chart *writes* the set and the tenant controller *reads*
it, and until they meet, a malformed NetworkPolicy or a mistyped placeholder is
discovered by the first tenant onboarding. CI runs this over a real
``helm template`` so the failure lands on the pull request instead.

    helm template serverless-api charts/serverless-api > rendered.yaml
    python dev/tenant_templates.py rendered.yaml > tenant-objects.yaml

Loading is the first half of the check - it applies every rule the running
tenant controller applies (kinds, placeholders, parseable YAML). Rendering is the
second: what it prints is what would land in a tenant namespace, ready for
kubeconform.

``--digest`` prints the set's hash alone. Rendering the chart for each region
and comparing the two is how the region-neutrality the provision fan-out depends
on gets asserted rather than assumed (tenant_controller/provision.py).
"""

from __future__ import annotations

import argparse
import sys

import yaml

from tenant_controller.templates import TemplateSet

# The mount whose ConfigMaps hold the set, and a plausible tenant to render it
# for. The group is arbitrary: rendering proves the placeholders resolve, not
# that any particular group exists.
VOLUME_NAME = "tenant-templates"
SAMPLE_GROUP = "payments"
SAMPLE_REGION = "central"
SAMPLE_REGISTRY = "registry.example.internal"


def template_set(rendered: str) -> TemplateSet:
    """The template set the chart ships, assembled the way the kubelet does.

    Follows the tenant controller's own projected volume rather than guessing
    at ConfigMap names, so a Deployment that projects a ConfigMap the chart
    does not render fails here instead of as a pod that will not mount.

    Args:
        rendered: A whole ``helm template`` output.

    Returns:
        The loaded set.

    Raises:
        SystemExit: If the render holds no such volume, or names a ConfigMap it
            does not contain. Silently checking nothing is the one outcome this
            script must not have.
    """
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    config_maps = {
        d["metadata"]["name"]: (d.get("data") or {}) for d in docs if d.get("kind") == "ConfigMap"
    }
    wanted = _projected_config_maps(docs)
    if not wanted:
        raise SystemExit(f"no {VOLUME_NAME!r} projected volume in the rendered chart")
    sources: dict[str, str] = {}
    for name in wanted:
        if name not in config_maps:
            raise SystemExit(
                f"the tenant controller projects ConfigMap {name!r}, "
                "which the chart does not render"
            )
        sources.update(config_maps[name])
    return TemplateSet.from_sources(sources.items())


def _projected_config_maps(docs: list) -> list[str]:
    """The ConfigMap names the tenant controller mounts as its template set."""
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        for volume in doc["spec"]["template"]["spec"].get("volumes") or []:
            if volume.get("name") != VOLUME_NAME:
                continue
            return [
                source["configMap"]["name"]
                for source in (volume.get("projected") or {}).get("sources") or []
                if "configMap" in source
            ]
    return []


def main() -> None:
    """Load the set, render it for a sample tenant, and print the manifests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rendered", help="a file holding `helm template` output, or - for stdin")
    parser.add_argument("--group", default=SAMPLE_GROUP)
    parser.add_argument(
        "--digest",
        action="store_true",
        help="print only the set's hash - two regions' charts must produce the same one",
    )
    args = parser.parse_args()

    text = sys.stdin.read() if args.rendered == "-" else open(args.rendered).read()  # noqa: SIM115
    templates = template_set(text)
    if args.digest:
        print(templates.digest)
        return
    manifests = templates.render(
        namespace=f"{args.group}-serverless",
        group=args.group,
        region=SAMPLE_REGION,
        registry=SAMPLE_REGISTRY,
    )
    print(f"# {len(templates)} template file(s), set {templates.digest}", file=sys.stderr)
    print(yaml.safe_dump_all(manifests, default_flow_style=False, sort_keys=False))


if __name__ == "__main__":
    main()
