"""Name validation and normalisation shared by every service.

``api.models.common`` re-exports the ``Annotated`` types, so request models and
query params keep importing them from there.
"""

from __future__ import annotations

import hashlib
import re
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator, WithJsonSchema

# DNS-1123 label: lowercase alphanumeric and '-', not starting or ending with '-'.
DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
# RFC-1123 hostname (FQDN): lowercase labels separated by dots, <=253 chars.
HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)+$"
)
# Leading "ggd-<1-4 digits>-" prefix some OIDC groups carry (e.g.
# "ggd-1234-platforms" is the group "platforms").
_GGD_PREFIX = re.compile(r"^ggd-\d{1,4}-")
_UNDERSCORE = str.maketrans({"_": "-"})

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


def normalize_group(group: str) -> str:
    """Normalize a group name to its bare, DNS-safe form."""
    return _GGD_PREFIX.sub("", group.lstrip("/").lower()).translate(_UNDERSCORE)


def validate_name(name: str) -> str:
    """Validate a workload name as a DNS-1123 label.

    Raises:
        ValueError: If it isn't a DNS-1123 label of at most 63 characters.
    """
    if not DNS1123.match(name) or len(name) > 63:
        raise ValueError(
            "name must be a DNS-1123 label (lowercase alphanumeric and '-', <=63 chars)"
        )
    return name


def validate_group(group: str) -> str:
    """Normalize and validate a group name as a DNS-1123 label.

    Normalization runs first, so a ``ggd-<digits>-`` prefix, "_" separators and
    upper case are accepted on input; the check applies to the normalized form.

    Raises:
        ValueError: If the normalized form isn't a DNS-1123 label of at most 63
            characters.
    """
    group = normalize_group(group)
    if not DNS1123.match(group) or len(group) > 63:
        raise ValueError(
            "group must be a DNS-1123 label (alphanumeric, '-' or '_', <=63 chars); "
            "'_' is normalized to '-' and the name is lowercased"
        )
    return group


def validate_hostname(host: str) -> str:
    """Validate a custom hostname as a DNS-1123 label or a lowercase FQDN.

    Raises:
        ValueError: If it is neither a DNS-1123 label nor a valid lowercase FQDN.
    """
    if (DNS1123.match(host) and len(host) <= 63) or HOSTNAME.match(host):
        return host
    raise ValueError("hostname must be a DNS-1123 label or a valid lowercase FQDN")


def validate_git_url(url: str) -> str:
    """Validate a source repository URL as http(s) with a host and no userinfo.

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

    Raises:
        ValueError: If it isn't a usable git ref.
    """
    if not branch or branch.strip() != branch or not branch.strip():
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


def validate_source_path(path: str) -> str:
    """Validate the directory inside the repository that holds the application.

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


def object_name(name: str, group: str) -> str:
    """The cluster name of a workload and everything derived from it."""
    return f"{name}-{group}"


def validate_object_name(name: str, group: str) -> str:
    """Check that ``{name}-{group}`` still fits where it has to be written.

    Raises:
        ValueError: If the pair exceeds 63 characters together.
    """
    oname = object_name(name, group)
    if len(oname) > MAX_OBJECT_NAME:
        raise ValueError(
            f"name and group are too long together: '{name}' + '{group}' is "
            f"{len(oname)} characters and the limit is {MAX_OBJECT_NAME} "
            f"(the name is used as a DNS label); shorten the name by "
            f"{len(oname) - MAX_OBJECT_NAME}"
        )
    return oname


def image_tag(branch: str) -> str:
    """Reduce a branch name to a legal OCI tag.

    A projection, not the branch: ``feature/login`` pushes to ``feature-login``
    and collides with a branch of that name. The revision is never rewritten, so
    the build still compiles the ref that was asked for. Never empty - a branch
    with no ASCII at all falls back to a digest of itself.
    """
    tag = _TAG_UNSAFE.sub("-", branch).lstrip(".-")[:_TAG_MAX]
    if not tag:
        return "b-" + hashlib.sha256(branch.encode()).hexdigest()[:12]
    return tag


def repository_of(image: str) -> str:
    """The repository half of an image reference - everything but tag and digest."""
    repo = image.split("@", 1)[0]
    head, sep, tail = repo.rpartition("/")
    return f"{head}{sep}{tail.split(':', 1)[0]}" if sep else repo.split(":", 1)[0]


def image_repository(group: str, name: str) -> str:
    """The repository a function's images are pushed to: ``{group}/{name}``.

    No projection needed, unlike the tag. Both parts are DNS-1123 labels, already
    a subset of what an OCI path component allows, so there is nothing to rewrite.
    """
    return f"{group}/{name}"


def cache_repository(group: str, name: str) -> str:
    """The repository a function's build cache is pushed to: ``{group}/{name}_cache``.

    The ``_`` is load-bearing: a name is a DNS-1123 label, so no function can be
    named ``{name}_cache``. A reserved tag would not be safe the same way - a
    branch named ``cache`` projects to exactly that.
    """
    return f"{image_repository(group, name)}{CACHE_SUFFIX}"


def _schema(description: str, example: str, **fields) -> WithJsonSchema:
    """Describe a validated string for OpenAPI, without constraining it."""
    return WithJsonSchema(
        {"type": "string", "description": description, "examples": [example], **fields}
    )


# Shared by request models, query params and the build contract. The group
# validator also NORMALIZES, so nothing downstream re-normalizes. Each carries a
# JSON Schema so /openapi.json publishes the rule instead of a bare "string";
# the patterns come from the same regexes the validators use.
Name = Annotated[
    str,
    AfterValidator(validate_name),
    _schema(
        "DNS-1123 label: lowercase alphanumeric and '-'.",
        "image-resizer",
        pattern=DNS1123.pattern,
        maxLength=63,
    ),
]
Group = Annotated[
    str,
    AfterValidator(validate_group),
    _schema(
        "Owning SSO group. Normalized before validation: a 'ggd-<digits>-' prefix "
        "is stripped, '_' becomes '-', and the name is lowercased.",
        "payments",
        maxLength=63,
    ),
]
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
