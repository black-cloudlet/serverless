"""Extract the chart's tenant template set and render it for one group.

The seam this closes: the chart *writes* the set and the provisioner *reads*
it, and until they meet, a malformed NetworkPolicy or a mistyped placeholder is
discovered by the first tenant onboarding. CI runs this over a real
``helm template`` so the failure lands on the pull request instead.

    helm template serverless-api charts/serverless-api > rendered.yaml
    python dev/tenant_templates.py rendered.yaml > tenant-objects.yaml

Loading is the first half of the check - it applies every rule the running
provisioner applies (kinds, placeholders, parseable YAML). Rendering is the
second: what it prints is what would land in a tenant namespace, ready for
kubeconform.

``--digest`` prints the set's hash alone. Rendering the chart for each region
and comparing the two is how the region-neutrality the ensure fan-out depends
on gets asserted rather than assumed (provisioner/ensure.py).
"""

from __future__ import annotations

import argparse
import sys

import yaml

from provisioner.templates import TemplateSet

# What the ConfigMap is called, and a plausible tenant to render it for. The
# group is arbitrary: rendering proves the placeholders resolve, not that any
# particular group exists.
CONFIG_MAP_SUFFIX = "-tenant-templates"
SAMPLE_GROUP = "payments"
SAMPLE_REGION = "central"
SAMPLE_REGISTRY = "registry.example.internal"


def template_set(rendered: str) -> TemplateSet:
    """The template set the chart ships, loaded the way the provisioner loads it.

    Args:
        rendered: A whole ``helm template`` output.

    Returns:
        The loaded set.

    Raises:
        SystemExit: If the render holds no tenant-templates ConfigMap - the
            chart changed shape, and silently checking nothing is the one
            outcome this script must not have.
    """
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") != "ConfigMap":
            continue
        if str(doc.get("metadata", {}).get("name", "")).endswith(CONFIG_MAP_SUFFIX):
            return TemplateSet.from_sources((doc.get("data") or {}).items())
    raise SystemExit(f"no *{CONFIG_MAP_SUFFIX} ConfigMap in the rendered chart")


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
