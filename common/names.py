"""Naming rules for what THIS platform builds out of a name and a group.

The platform-wide part - what a name and a group may be, and how a group is
normalized - lives in :mod:`cloudlet_apis.names` and is re-exported below, so
every API agrees on it. What stays here is what this platform derives on its own:
object names, image and cache repositories, the OCI tag projected from a revision,
and the git/image/path validators.

``api.models.common`` re-exports the ``Annotated`` types, so request models and
query params keep importing them from there.
"""

from __future__ import annotations

import hashlib
import re
from typing import Annotated
from urllib.parse import urlsplit

# The platform-wide rules, re-exported so this module is the one import site for
# naming in this repository (see the module docstring). DNS1123 is imported, so
# there is a single copy of that regex.
from cloudlet_apis.names import (  # noqa: F401
    DNS1123,
    Group,
    Name,
    normalize_group,
    validate_group,
    validate_name,
)
from pydantic import AfterValidator, WithJsonSchema

# DNS-1123 *subdomain* - Kubernetes' rule for a pod name: dot-separated
# DNS-1123 labels, at most MAX_POD_NAME characters.
DNS1123_SUBDOMAIN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
MAX_POD_NAME = 253

# RFC-1123 hostname (FQDN): lowercase labels separated by dots, <=253 chars.
HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)+$"
)
# Characters an OCI tag may not contain; the tag must also start alphanumeric
# or '_' and is capped at 128 characters.
_TAG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_TAG_MAX = 128
# Distinguishes a function's cache repository from its image repository.
CACHE_SUFFIX = "_cache"

# An image reference, per the OCI distribution grammar:
#   [domain[:port]/]path[/path...][:tag][@algorithm:hex]
# Path components are lowercase - the registry rejects anything else - while a
# tag may carry upper case.
_IMG_DOMAIN_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_IMG_DOMAIN = rf"{_IMG_DOMAIN_LABEL}(?:\.{_IMG_DOMAIN_LABEL})*(?::[0-9]{{1,5}})?"
_IMG_PATH_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_IMG_TAG = r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}"
_IMG_DIGEST = r"[A-Za-z][A-Za-z0-9]*(?:[-_+.][A-Za-z][A-Za-z0-9]*)*:[0-9a-fA-F]{32,}"
IMAGE_REFERENCE = re.compile(
    rf"^(?:{_IMG_DOMAIN}/)?{_IMG_PATH_COMPONENT}(?:/{_IMG_PATH_COMPONENT})*"
    rf"(?::{_IMG_TAG})?(?:@{_IMG_DIGEST})?$"
)
# The distribution spec caps a repository name at 255; a digest adds ~80 more.
_IMAGE_MAX = 512

# A DNS label's cap, which the first label of the default host has to fit.
# A workload's own name is capped by `validate_name` at the same 63; this rule
# bounds the name and group PAIR, which appears only in that label - see
# `default_host_label`.
MAX_HOST_LABEL = 63

# A Namespace name is a DNS-1123 label, so it shares the 63-character cap.
MAX_NAMESPACE_NAME = 63
# Tenant namespaces are `{group}-serverless`: the group first, then the suffix,
# which keeps any group from naming an existing cluster namespace.
NAMESPACE_SUFFIX = "-serverless"
# A group with one of these prefixes would produce a namespace that reads as the
# system's own, so `namespace_for_group` refuses it.
_RESERVED_NAMESPACE_PREFIXES = ("kube-", "openshift-")

# An environment variable name, exactly as Kubernetes accepts one
# (`util/validation.IsEnvVarName`). It is also used verbatim as the key of the
# workload's `{workload}-env` Secret, and this is a subset of what a Secret key
# allows, so one rule covers both writers. The 253-character cap is that Secret
# key's; Kubernetes puts no length limit on a container env name itself.
ENV_VAR_NAME = re.compile(r"^[-._a-zA-Z][-._a-zA-Z0-9]*$")
MAX_ENV_VAR_NAME = 253
# A ConfigMap/Secret key is capped here, and a mount path becomes one (see
# `api.services.manifests.files._key`).
MAX_MOUNT_PATH = 253


def validate_hostname(host: str) -> str:
    """Validate a custom hostname as a DNS-1123 label or a lowercase FQDN.

    Either a single DNS-1123 label (the platform base domain is appended by the
    API) or a full lowercase FQDN. That the FQDN sits under the platform base
    domain is enforced in the service layer, where the base domain is known.

    Args:
        host: The candidate hostname.

    Returns:
        The host unchanged.

    Raises:
        ValueError: If it is neither a DNS-1123 label nor a valid lowercase FQDN.
    """
    if (DNS1123.match(host) and len(host) <= 63) or HOSTNAME.match(host):
        return host
    raise ValueError(
        "hostname must be a single lowercase label (letters, digits and '-') "
        "or a full lowercase domain name like app.example.com"
    )


def validate_git_url(url: str) -> str:
    """Validate a source repository URL as http(s) with a host and no userinfo.

    The clone authenticates with a basic-auth Secret, which applies only over
    http(s), so a scheme other than http/https, an scp-style ref (which carries
    no scheme), a missing host and an empty URL are all refused.

    Embedded credentials are rejected, not stripped: the URL is written verbatim
    to ``Image.spec.source.git.url``, an object with much wider read access than
    the Secret. The token travels as ``gitToken``
    (docs/BUILDING.md - Git credential - per function, never shared).

    Args:
        url: The repository URL.

    Returns:
        The URL unchanged.

    Raises:
        ValueError: If it is not an http(s) URL with a host, or carries userinfo.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(
            "gitRepo must be an http(s) URL (e.g. https://git.internal/org/repo.git); "
            "SSH and scp-style refs are not supported - the build authenticates "
            "with a token over https"
        )
    if "@" in parts.netloc:
        raise ValueError("gitRepo must not embed credentials; send the token as gitToken instead")
    return url.strip()


def validate_image_ref(image: str) -> str:
    """Validate a container image reference the caller wants deployed.

    The value is written verbatim to ``containers[0].image`` and is parsed by
    :func:`api.services.manifests.secrets.registry_of` to key the pull secret.

    Enforces the OCI distribution grammar: an optional ``domain[:port]``, one or
    more lowercase path components, and an optional ``:tag`` and/or
    ``@algorithm:hex`` digest, in at most ``_IMAGE_MAX`` characters. Surrounding
    whitespace is stripped; whitespace inside is rejected.

    A reference with no tag is accepted and means ``:latest``.

    Args:
        image: The image reference.

    Returns:
        The reference, stripped of surrounding whitespace.

    Raises:
        ValueError: If it is empty or not a well-formed reference.
    """
    cleaned = image.strip()
    if not cleaned:
        raise ValueError("image must not be empty")
    if len(cleaned) > _IMAGE_MAX:
        raise ValueError(f"image must be at most {_IMAGE_MAX} characters")
    if not IMAGE_REFERENCE.match(cleaned):
        raise ValueError(
            "image must be a container image reference like "
            "'registry.internal/team/app:1.2.3' (optional registry and port, "
            "lowercase path, optional ':tag' and/or '@sha256:...' digest)"
        )
    return cleaned


def validate_revision(revision: str) -> str:
    """Validate a git revision - a branch, a tag, or a commit SHA.

    All three are the same thing to git and to kpack: a value for
    ``Image.spec.source.git.revision``. This checks only that it is a usable
    ref, never which kind it is; the platform does not care, and asking would
    mean a network round trip to the repository.

    ``/`` is permitted and kept verbatim as the git revision; only the derived
    image tag cannot hold one, and :func:`image_tag` projects it separately.

    Rejects what git itself rejects and what would be unsafe downstream: empty
    or whitespace-only, whitespace or control characters anywhere, a leading
    ``-`` (reads as a flag), the sequences git forbids in a ref, and anything
    over 255 characters.

    Args:
        revision: The candidate branch, tag or commit.

    Returns:
        The revision unchanged.

    Raises:
        ValueError: If it isn't a usable git ref.
    """
    if not revision or revision.strip() != revision:
        raise ValueError("revision must not be empty or padded with whitespace")
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in revision):
        raise ValueError("revision must not contain whitespace or control characters")
    if revision.startswith("-") or revision.endswith("/") or revision.endswith(".lock"):
        raise ValueError("revision must not start with '-' or end with '/' or '.lock'")
    if ".." in revision or "//" in revision or any(c in revision for c in "~^:?*[\\"):
        raise ValueError("revision contains a sequence git does not allow in a ref")
    if len(revision) > 255:
        raise ValueError("revision must be at most 255 characters")
    return revision


def validate_pod_name(pod: str) -> str:
    """Validate a pod name taken from the request path.

    The value is interpolated into a request to the cluster's API server, so it
    is constrained at the edge, before any service sees it, to what Kubernetes
    itself accepts as a pod name: a ``..`` or a ``/`` in a path segment would
    address a resource other than the one named.

    It is not an authorization check: whether the pod is *this workload's* is
    decided against its labels.

    Args:
        pod: The pod name from the path.

    Returns:
        The name unchanged.

    Raises:
        ValueError: If it is empty, over-long, or not a DNS-1123 subdomain.
    """
    if not pod:
        raise ValueError("pod name is required")
    if len(pod) > MAX_POD_NAME:
        raise ValueError(f"pod name must be at most {MAX_POD_NAME} characters")
    if not DNS1123_SUBDOMAIN.match(pod):
        raise ValueError("pod name may use only lowercase letters, digits, '-' and '.'")
    return pod


def validate_source_path(path: str) -> str:
    """Validate the directory inside the repository that holds the application.

    Surrounding ``/`` are stripped rather than rejected, so "/src" and "src"
    name the same directory. ".." is refused: kpack resolves the path inside the
    clone, and escaping it would build something other than the repository.

    Args:
        path: A repository-relative directory; "" is the repository root.

    Returns:
        The path without surrounding "/", or "" for the root.

    Raises:
        ValueError: If it escapes the repository or holds whitespace, a
            backslash, or an empty segment.
    """
    cleaned = path.strip().strip("/")
    if not cleaned:
        return ""
    if "\\" in cleaned or any(c.isspace() or ord(c) < 0x20 for c in cleaned):
        raise ValueError("path must not contain whitespace or '\\'")
    if any(seg in ("", ".", "..") for seg in cleaned.split("/")):
        raise ValueError("path must not contain empty, '.' or '..' segments")
    if len(cleaned) > 255:
        raise ValueError("path must be at most 255 characters")
    return cleaned


def validate_env_var_name(name: str) -> str:
    """Validate an environment variable name.

    The value is written verbatim into the container's ``env`` **and**, for a
    secret var, used as the key of the ``{workload}-env`` Secret, so this one
    rule has to satisfy both writers.

    The rule is Kubernetes' own ``IsEnvVarName``: ``[-._a-zA-Z][-._a-zA-Z0-9]*``,
    which cannot start with a digit and, since it may not begin with ``.``
    followed by nothing else, can never be ``.`` or ``..`` - the two keys a
    ConfigMap and a Secret both reject.

    Args:
        name: The candidate variable name.

    Returns:
        The name unchanged.

    Raises:
        ValueError: If it is empty, over-long, or not a legal environment
            variable name.
    """
    if not name:
        raise ValueError("env var name must not be empty")
    if len(name) > MAX_ENV_VAR_NAME:
        raise ValueError(f"env var name must be at most {MAX_ENV_VAR_NAME} characters")
    if name.startswith(".."):
        raise ValueError("env var name must not start with '..'")
    if not ENV_VAR_NAME.match(name):
        raise ValueError(
            "env var name must be letters, digits, '-', '_' or '.', and must not "
            "start with a digit (e.g. 'LOG_LEVEL', 'my.env-name')"
        )
    return name


def validate_mount_path(path: str) -> str:
    """Validate the path a file is mounted at inside the container.

    Two consumers set the rule. Kubernetes requires a non-empty ``mountPath``
    containing no ``:``; and the path is projected into the backing
    ConfigMap/Secret key the file is stored under
    (``api.services.manifests.files._key``), which is capped at
    ``MAX_MOUNT_PATH`` and cannot address a parent directory.

    ``..`` is refused, not normalised: the mount is a ``subPath`` of a shared
    volume, so a path escaping it would not mean what it says.

    Args:
        path: The candidate mount path.

    Returns:
        The path stripped of surrounding whitespace.

    Raises:
        ValueError: If it is empty, too long, holds ``:``/control characters, or
            contains a ``..`` segment.
    """
    cleaned = path.strip()
    if not cleaned:
        raise ValueError("mountPath must not be empty")
    if len(cleaned) > MAX_MOUNT_PATH:
        raise ValueError(f"mountPath must be at most {MAX_MOUNT_PATH} characters")
    if ":" in cleaned:
        raise ValueError("mountPath must not contain ':'")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in cleaned):
        raise ValueError("mountPath must not contain control characters")
    if any(seg == ".." for seg in cleaned.split("/")):
        raise ValueError("mountPath must not contain a '..' segment")
    return cleaned


def default_host_label(name: str, group: str) -> str:
    """The first label of a workload's default host: ``{name}-{group}``.

    The one place the pair is composed. DNS is global, so two groups' ``app``
    cannot both be ``app.{routeDomain}``; the platform's wildcard certificate
    covers exactly one label, so the host stays one label deep
    (docs/ARCHITECTURE.md - Route host convention (recommendation)).

    Args:
        name: The workload name.
        group: The owning group.

    Returns:
        The label, without the route domain.
    """
    return f"{name}-{group}"


def validate_default_host_label(name: str, group: str, limit: int = MAX_HOST_LABEL) -> str:
    """Check that the default host's first label fits, and return it.

    The pair is written in exactly one place: the default host's first label.
    A workload's cluster name is plain ``{name}``, scoped by its group's
    namespace, so the label is the only thing the pair's length bounds. Over
    the limit means only that the *default* host will not fit, and a
    caller-supplied ``hostname`` is the way through.

    Args:
        name: The workload name.
        group: The owning group.
        limit: The cap on the label.

    Returns:
        The label.

    Raises:
        ValueError: If the pair exceeds ``limit`` together.
    """
    label = default_host_label(name, group)
    if len(label) > limit:
        raise ValueError(
            f"name and group are too long together for the default host: "
            f"'{label}' is {len(label)} characters and the limit is {limit}; "
            f"shorten the name by {len(label) - limit}, or pass a hostname"
        )
    return label


def namespace_for_group(group: str, suffix: str = NAMESPACE_SUFFIX) -> str:
    """The namespace a group's workloads live in: ``{group}{suffix}``.

    One home for the mapping: the API, the tenant controller and the GC derive
    the same name from here. The group arrives normalized; the checks here are
    the namespace's own, on the suffixed whole.

    Args:
        group: The normalized owning group.
        suffix: The tenant-namespace suffix (configurable via the chart).

    Returns:
        The namespace name.

    Raises:
        ValueError: If the result is too long, ill-formed, or starts with a
            reserved system prefix.
    """
    namespace = f"{group}{suffix}"
    if len(namespace) > MAX_NAMESPACE_NAME:
        raise ValueError(
            f"group '{group}' is too long: with the '{suffix}' suffix the "
            f"namespace is {len(namespace)} characters and the limit is "
            f"{MAX_NAMESPACE_NAME}; shorten the group by "
            f"{len(namespace) - MAX_NAMESPACE_NAME}"
        )
    if not DNS1123.match(namespace):
        raise ValueError(
            "group may use only lowercase letters, digits and '-', "
            "and must start and end with a letter or digit"
        )
    reserved = next((p for p in _RESERVED_NAMESPACE_PREFIXES if namespace.startswith(p)), None)
    if reserved:
        raise ValueError(f"group must not start with '{reserved}' (reserved for system namespaces)")
    return namespace


def image_tag(revision: str) -> str:
    """Reduce a git revision to a legal OCI tag.

    A git ref may contain ``/``; an OCI tag may not, and must start with an
    alphanumeric or ``_`` and fit in ``_TAG_MAX`` characters. The tag is
    therefore a *projection* of the revision, not the revision itself -
    ``feature/login`` builds from that exact ref but pushes to ``feature-login``.

    Two revisions differing only in replaced characters land on one tag
    (``feature/login`` and ``feature-login``). The revision is never rewritten,
    so a build always compiles the ref that was asked for.

    A revision can also project to *nothing*: git refs are UTF-8, so one with no
    ASCII is legal and every character of it is replaced. The empty tag would
    make the reference ``repo:``, so those fall back to ``b-`` plus a digest of
    the revision, which is deterministic for a given revision.

    Args:
        revision: The branch, tag or commit (already validated).

    Returns:
        The tag to push to, never empty.
    """
    tag = _TAG_UNSAFE.sub("-", revision).lstrip(".-")[:_TAG_MAX]
    if not tag:
        return "b-" + hashlib.sha256(revision.encode()).hexdigest()[:12]
    return tag


def repository_of(image: str) -> str:
    """The repository half of an image reference - everything but tag and digest.

    ``reg/acme/team/app:main``, ``reg/acme/team/app@sha256:...`` and
    ``reg/acme/team/app:main@sha256:...`` all reduce to ``reg/acme/team/app``.

    The tag is split off only after the digest, and only past the last ``/``: a
    registry host may carry a port, and that colon is not a tag separator.

    Args:
        image: An image reference (already validated).

    Returns:
        The repository, including host.
    """
    repo = image.split("@", 1)[0]
    head, sep, tail = repo.rpartition("/")
    return f"{head}{sep}{tail.split(':', 1)[0]}" if sep else repo.split(":", 1)[0]


def tag_of(image: str) -> str | None:
    """The tag half of an image reference, or None when it carries none.

    The counterpart of :func:`repository_of`, splitting the same grammar the
    same way: the digest is cut first, and a ``:`` counts as a tag separator
    only past the last ``/`` - a registry host may carry a port.

    Args:
        image: An image reference (already validated).

    Returns:
        The tag, or None for a bare or digest-only reference.
    """
    untagged = image.split("@", 1)[0]
    tail = untagged.rpartition("/")[2]
    if ":" not in tail:
        return None
    return tail.split(":", 1)[1]


def digest_of(image: str | None) -> str | None:
    """The digest an image reference pins, or None when it names none.

    Args:
        image: An image reference, or None.

    Returns:
        The ``algorithm:hex`` half - the form a registry's tag listing reports
        as the manifest digest, so the two compare directly.
    """
    if image and "@" in image:
        return image.rsplit("@", 1)[1]
    return None


def image_repository(group: str, name: str) -> str:
    """The repository a function's images are pushed to: ``{group}/{name}``.

    The other half of an image reference from :func:`image_tag`: the code that
    pushes to a repository and the code that deletes one derive it here.

    Nothing is projected, unlike the tag. Both parts are DNS-1123 labels, already
    a subset of what an OCI path component allows, so there is nothing to rewrite.

    Args:
        group: The owning group.
        name: The workload name.

    Returns:
        The repository path, below the registry base.
    """
    return f"{group}/{name}"


def cache_repository(group: str, name: str) -> str:
    """The repository a function's build cache is pushed to: ``{group}/{name}_cache``.

    The ``_`` makes a collision with the image repository impossible: a name is
    a DNS-1123 label admitting only ``[a-z0-9-]``, so no function can be named
    ``{name}_cache`` (docs/RUNTIMES.md - Build cache).

    Args:
        group: The owning group.
        name: The workload name.

    Returns:
        The cache repository path, below the registry base.
    """
    return f"{image_repository(group, name)}{CACHE_SUFFIX}"


def _schema(description: str, example: str, **fields) -> WithJsonSchema:
    """Describe a validated string for OpenAPI, without constraining it.

    ``WithJsonSchema`` documents; it does not validate. A real ``pattern`` runs
    BEFORE the AfterValidator, so it would reject "My_Team" before
    :func:`normalize_group` canonicalises it, and "/src" before
    :func:`validate_source_path` strips it. The validator is the only authority;
    this is what a client generates code from.

    Args:
        description: What the field means, shown in the generated client.
        example: A value that passes.
        **fields: Extra JSON Schema keys (``pattern``, ``maxLength``, ...).

    Returns:
        The schema annotation to add to an ``Annotated`` chain.
    """
    return WithJsonSchema(
        {"type": "string", "description": description, "examples": [example], **fields}
    )


# Shared by request models, query params and the build contract, alongside the
# Name/Group pair re-exported from cloudlet_apis.names above. Each carries a JSON
# Schema so /openapi.json publishes the rule instead of a bare "string"; the
# patterns come from the same regexes the validators use.
Hostname = Annotated[
    str,
    AfterValidator(validate_hostname),
    _schema(
        "Custom host: one lowercase label (letters, digits and '-'), or a full "
        "domain name one label under the platform route domain.",
        "checkout",
        maxLength=253,
    ),
]
Revision = Annotated[
    str,
    AfterValidator(validate_revision),
    _schema("Branch, tag or commit to build.", "main", maxLength=255),
]
GitUrl = Annotated[
    str,
    AfterValidator(validate_git_url),
    _schema(
        "Repository URL. http(s) only, with no embedded credentials - the token goes in gitToken.",
        "https://git.internal/payments/hello.git",
        pattern=r"^https?://",
    ),
]
ImageRef = Annotated[
    str,
    AfterValidator(validate_image_ref),
    # No `pattern`: the validator strips surrounding whitespace first, so a
    # pattern running ahead of it would reject what it fixes (as for Group/SourcePath).
    _schema(
        "Container image reference: optional 'registry[:port]/', lowercase path, "
        "optional ':tag' and/or '@sha256:...' digest. No tag means ':latest'.",
        "registry.internal/payments/checkout:1.2.3",
        maxLength=_IMAGE_MAX,
    ),
]
SourcePath = Annotated[
    str,
    AfterValidator(validate_source_path),
    _schema(
        "Directory inside the repository holding the application; empty builds "
        "the repository root. Surrounding '/' are stripped and '..' is rejected.",
        "services/api",
        maxLength=255,
    ),
]
PodName = Annotated[
    str,
    AfterValidator(validate_pod_name),
    _schema(
        "A pod name, exactly as the pods stream reported it.",
        "orders-team-00001-deployment-6b9f4c5d7-x2wql",
        pattern=DNS1123_SUBDOMAIN.pattern,
        maxLength=MAX_POD_NAME,
    ),
]
EnvVarName = Annotated[
    str,
    AfterValidator(validate_env_var_name),
    _schema(
        "Environment variable name. Letters, digits, '-', '_' and '.'; may not start "
        "with a digit. A secret var is stored under this same key.",
        "LOG_LEVEL",
        pattern=ENV_VAR_NAME.pattern,
        maxLength=MAX_ENV_VAR_NAME,
    ),
]
MountPath = Annotated[
    str,
    AfterValidator(validate_mount_path),
    # No `pattern`: the validator strips surrounding whitespace first, so a
    # pattern running ahead of it would reject what it fixes (as for Group/SourcePath).
    _schema(
        "Path the file is mounted at inside the container. Must not be empty, "
        "contain ':', or contain a '..' segment.",
        "/etc/app/config.yaml",
        maxLength=MAX_MOUNT_PATH,
    ),
]
