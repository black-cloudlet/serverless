"""Naming rules for what THIS platform builds out of a name and a group.

The platform-wide part - what a name and a group may be, and how a group is
normalized - lives in :mod:`cloudlet_apis.names` and is re-exported below, since
every API has to agree on it. What stays here is what only we derive: object
names, image and cache repositories, the OCI tag projected from a branch, and the
git/image/path validators. Those change when the build pipeline changes.

``api.models.common`` re-exports the ``Annotated`` types, so request models and
query params keep importing them from there.
"""

from __future__ import annotations

import hashlib
import re
from typing import Annotated
from urllib.parse import urlsplit

# The platform-wide rules, re-exported so this module stays the one import region
# for naming in this repository (see the module docstring). DNS1123 is imported
# rather than redeclared: a second copy of the regex is a second thing to drift.
from cloudlet_apis.names import (  # noqa: F401
    DNS1123,
    Group,
    Name,
    normalize_group,
    validate_group,
    validate_name,
)
from pydantic import AfterValidator, WithJsonSchema

# DNS-1123 *subdomain* - Kubernetes' rule for a pod name. Dotted labels are
# permitted because the rule permits them, not because Knative produces one.
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
# tag may carry upper case. Assembled from the parts so each rule is legible.
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

# A KSVC name and a DNS label are both capped here, and {name}-{group} is both.
MAX_OBJECT_NAME = 63

# An environment variable name, exactly as Kubernetes accepts one
# (`util/validation.IsEnvVarName`). It is also used verbatim as the key of the
# workload's `{workload}-env` Secret, and this is a subset of what a Secret key
# allows, so one rule covers both writers.
ENV_VAR_NAME = re.compile(r"^[-._a-zA-Z][-._a-zA-Z0-9]*$")
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
    raise ValueError("hostname must be a DNS-1123 label or a valid lowercase FQDN")


def validate_git_url(url: str) -> str:
    """Validate a source repository URL as http(s) with a host and no userinfo.

    The clone authenticates with a basic-auth Secret, which only applies over
    http(s). An scp-style ref has no scheme, so it becomes a ``kpack.io/git``
    annotation kpack cannot match, failing as an auth error nowhere near its cause.
    An empty URL passes every downstream check and fails the same way.

    Embedded credentials are rejected rather than stripped: the URL is written
    verbatim to ``Image.spec.source.git.url``, so a password in it would sit in
    plaintext on an object with much wider read access than the Secret. The
    token belongs in the Secret, which is where the caller already sends it.

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
    :func:`api.services.manifests.secrets.registry_of` to key the pull secret, so an
    unusable reference passes every check here and surfaces minutes later as a
    bare ``ErrImagePull`` on a revision - the failure mode the other validators
    in this module exist to prevent.

    Enforces the OCI distribution grammar: an optional ``domain[:port]``, one or
    more lowercase path components, and an optional ``:tag`` and/or
    ``@algorithm:hex`` digest. Surrounding whitespace is stripped (a value pasted
    from a console usually carries some); whitespace *inside* is rejected, since
    a reference cannot contain any and its presence means the field holds
    something other than an image.

    A reference with no tag is accepted and means ``:latest``, as it does
    everywhere else - that it is a poor thing to deploy is a policy question,
    not a well-formedness one.

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


def validate_branch(branch: str) -> str:
    """Validate a git branch name.

    Deliberately permissive about ``/``, which is ordinary in a branch name and
    is kept verbatim as the git revision. It is only the derived image tag that
    cannot hold one, and :func:`image_tag` handles that separately - rejecting
    ``feature/login`` here would ban a naming convention most repositories use.

    Rejects what git itself rejects and what would be unsafe downstream: empty
    or whitespace-only, whitespace or control characters anywhere, a leading
    ``-`` (reads as a flag), and the sequences git forbids in a ref.

    Args:
        branch: The candidate branch name.

    Returns:
        The branch unchanged.

    Raises:
        ValueError: If it isn't a usable git ref.
    """
    if not branch or branch.strip() != branch:
        raise ValueError("branch must not be empty or padded with whitespace")
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in branch):
        raise ValueError("branch must not contain whitespace or control characters")
    if branch.startswith("-") or branch.endswith("/") or branch.endswith(".lock"):
        raise ValueError("branch must not start with '-' or end with '/' or '.lock'")
    if ".." in branch or "//" in branch or any(c in branch for c in "~^:?*[\\"):
        raise ValueError("branch contains a sequence git does not allow in a ref")
    if len(branch) > 255:
        raise ValueError("branch must be at most 255 characters")
    return branch


def validate_pod_name(pod: str) -> str:
    """Validate a pod name taken from the request path.

    Not decoration. This value is interpolated into a request to the cluster's
    API server, so it is the one caller-supplied string on the read path that
    could reach somewhere other than the resource it names - ``..`` or a ``/``
    in a path segment is how a get becomes a get of something else. Constraining
    it to what Kubernetes itself accepts as a pod name settles that at the edge,
    before any service sees it.

    It is not an authorization check and does not pretend to be: whether the pod
    is *this workload's* is decided against its labels, where the answer is.

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
        raise ValueError(
            "pod name must be a DNS-1123 subdomain: lowercase alphanumeric, '-' and '.'"
        )
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

    Here for the same reason :func:`validate_image_ref` is: the value is written
    verbatim into the container's ``env`` **and**, for a secret var, used as the
    key of the ``{workload}-env`` Secret. Unchecked, a name the API server
    refuses is accepted (202) and dies in the background apply as a per-region
    error about a field the caller cannot see - the failure mode the validators
    in this module exist to prevent.

    The rule is Kubernetes' own ``IsEnvVarName``: ``[-._a-zA-Z][-._a-zA-Z0-9]*``,
    which cannot start with a digit and, since it may not begin with ``.``
    followed by nothing else, can never be ``.`` or ``..`` - the two keys a
    ConfigMap and a Secret both reject.

    Args:
        name: The candidate variable name.

    Returns:
        The name unchanged.

    Raises:
        ValueError: If it is empty or not a legal environment variable name.
    """
    if not name:
        raise ValueError("env var name must not be empty")
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

    Two consumers, both of which reject silently late. Kubernetes requires a
    non-empty ``mountPath`` containing no ``:``; and the path is projected into
    the backing ConfigMap/Secret key the file is stored under
    (``api.services.manifests.files._key``), which is capped and cannot address
    a parent directory.

    ``..`` is refused rather than normalised: the mount is a ``subPath`` of a
    shared volume, so a path escaping it would not mean what it says.

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


def object_name(name: str, group: str) -> str:
    """The cluster name of a workload and everything derived from it.

    ``{name}-{group}`` is the platform's primary key: the KSVC, its config Secrets,
    its git Secret and its kpack Image all hang off it, and a service starting from
    an Image has to get back to the KSVC. Written once, here.

    Args:
        name: The workload name.
        group: The owning group.

    Returns:
        The derived object name.
    """
    return f"{name}-{group}"


def validate_object_name(name: str, group: str, limit: int = MAX_OBJECT_NAME) -> str:
    """Check that ``{name}-{group}`` still fits where it has to be written.

    Each half is a legal DNS-1123 label, but the primary key is their concatenation,
    and that becomes the KSVC name and the first label of the host. Both are capped
    at 63, so two 63-character halves are individually valid and produce a
    127-character name the cluster rejects. The pair has to be checked as a pair.

    Args:
        name: The workload name.
        group: The owning group.
        limit: The cap on the pair; an offering that prefixes the pair somewhere
            a label value has to fit (a function's build objects) passes a
            tighter one.

    Returns:
        The derived object name.

    Raises:
        ValueError: If the pair exceeds ``limit`` characters together.
    """
    oname = object_name(name, group)
    if len(oname) > limit:
        raise ValueError(
            f"name and group are too long together: '{name}' + '{group}' is "
            f"{len(oname)} characters and the limit is {limit} "
            f"(the name is used as a DNS label); shorten the name by "
            f"{len(oname) - limit}"
        )
    return oname


def image_tag(branch: str) -> str:
    """Reduce a branch name to a legal OCI tag.

    A git branch may contain ``/``; an OCI tag may not, and must start with an
    alphanumeric or ``_``. So the tag is a *projection* of the branch, not the
    branch itself - ``feature/login`` builds from that exact ref but pushes to
    ``feature-login``.

    Two branches differing only in replaced characters collide on one tag
    (``feature/login`` and ``feature-login``). Accepted: the alternative is a tag
    nobody can read in a registry listing. The revision is never rewritten, so a
    build always compiles the branch that was asked for.

    A branch can also project to *nothing*: git refs are UTF-8, so one with no ASCII
    is legal and every character of it is replaced. The empty tag would make the
    reference ``repo:``, so those fall back to a digest of the branch - unreadable,
    but deterministic, which is what convergence needs.

    Args:
        branch: The branch name (already validated).

    Returns:
        The tag to push to, never empty.
    """
    tag = _TAG_UNSAFE.sub("-", branch).lstrip(".-")[:_TAG_MAX]
    if not tag:
        return "b-" + hashlib.sha256(branch.encode()).hexdigest()[:12]
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
    only past the last ``/`` - a registry host may carry a port. Here, beside
    the functions it mirrors, so a fix to one edge cannot miss the other.

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

    The other half of an image reference from :func:`image_tag`, and here for the
    same reason: it is a naming rule, and the code that pushes to a repository and
    the code that deletes one must agree on it exactly.

    No projection needed, unlike the tag. Both parts are DNS-1123 labels, already
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

    The ``_`` is what makes a collision with the image repository impossible
    rather than unlikely: a name is a DNS-1123 label admitting only ``[a-z0-9-]``,
    so no function can be named ``{name}_cache``. A reserved *tag* in the image
    repository would not be safe the same way - a branch named ``cache`` projects
    to exactly that tag (:func:`image_tag`) - and neither would a nested
    ``{name}/cache`` path, which adds a repository level that Quay accepts only
    with extended repository names enabled.

    Args:
        group: The owning group.
        name: The workload name.

    Returns:
        The cache repository path, below the registry base.
    """
    return f"{image_repository(group, name)}{CACHE_SUFFIX}"


def _schema(description: str, example: str, **fields) -> WithJsonSchema:
    """Describe a validated string for OpenAPI, without constraining it.

    ``WithJsonSchema`` documents; it does not validate. That is the point: a real
    ``pattern`` runs BEFORE the AfterValidator, so it would reject "My_Team"
    before :func:`normalize_group` could canonicalise it, and "/src" before
    :func:`validate_source_path` could strip it. The validator stays the only
    authority; this is what a client generates code from.

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
        "Custom host: one DNS-1123 label, or a lowercase FQDN one label under the "
        "platform route domain.",
        "checkout",
        maxLength=253,
    ),
]
Branch = Annotated[
    str,
    AfterValidator(validate_branch),
    _schema("Git branch or ref to build.", "main", maxLength=255),
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
        "A pod name, as the pods stream reported it. DNS-1123 subdomain.",
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
