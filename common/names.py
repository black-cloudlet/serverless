"""Name validation and normalisation shared by every service.

These live in ``common`` rather than in the API's request models because the
same rules bound what can be written to a cluster: a workload name becomes a
Kubernetes object name, a group becomes part of an image repository, a branch
becomes an image tag. Anything constructing those - the API at its HTTP edge,
the builder from a ``BuildRequest`` - has to agree on them.

``api.models.common`` re-exports the ``Annotated`` types, so request models and
query params keep importing them from there.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator

# DNS-1123 label: lowercase alphanumeric and '-', not starting or ending with '-'.
DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
# RFC-1123 hostname (FQDN): lowercase labels separated by dots, <=253 chars.
HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)+$"
)
# Leading "ggd-<1-4 digits>-" prefix some OIDC groups carry (e.g.
# "ggd-1234-platforms" is the group "platforms").
_GGD_PREFIX = re.compile(r"^ggd-\d{1,4}-")

# Characters an OCI tag may not contain; the tag must also start alphanumeric
# or '_' and is capped at 128 characters.
_TAG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_TAG_MAX = 128


def normalize_group(group: str) -> str:
    """Normalize a group name to its bare form.

    Strips the Keycloak path prefix ("/") and a leading ``ggd-<1-4 digits>-``
    prefix, so e.g. "/ggd-1234-platforms" and "platforms" name the same group.
    Applied both to groups from the OIDC token and to a request-supplied group.

    Args:
        group: The raw group name.

    Returns:
        The normalized group name.
    """
    return _GGD_PREFIX.sub("", group.lstrip("/"))


def validate_name(name: str) -> str:
    """Validate a workload name as a DNS-1123 label.

    Args:
        name: The candidate workload name.

    Returns:
        The name unchanged.

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

    Args:
        group: The candidate group name (a ``ggd-<digits>-`` prefix is stripped).

    Returns:
        The normalized group name.

    Raises:
        ValueError: If it isn't a DNS-1123 label of at most 63 characters.
    """
    group = normalize_group(group)
    if not DNS1123.match(group) or len(group) > 63:
        raise ValueError(
            "group must be a DNS-1123 label (lowercase alphanumeric and '-', <=63 chars)"
        )
    return group


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


def image_tag(branch: str) -> str:
    """Reduce a branch name to a legal OCI tag.

    A git branch may contain ``/``; an OCI tag may not, and must start with an
    alphanumeric or ``_``. So the tag is a *projection* of the branch, not the
    branch itself - ``feature/login`` builds from that exact ref but pushes to
    ``feature-login``.

    Two branches differing only in characters that get replaced would collide on
    one tag (``feature/login`` and ``feature-login``). That is accepted: the
    alternative is an encoded tag nobody can read in a registry listing, and the
    pair is unusual in one repository. The git revision is never rewritten, so a
    build always compiles the branch that was asked for.

    Args:
        branch: The branch name (already validated).

    Returns:
        The tag to push to.
    """
    tag = _TAG_UNSAFE.sub("-", branch).lstrip(".-")
    return tag[:_TAG_MAX]


# Validated string types shared by request models, query params and the build
# contract. The group validator also NORMALIZES ("/ggd-1234-team" -> "team"), so
# every group entering the app is already in bare, canonical form at the edge -
# nothing downstream re-normalizes.
Name = Annotated[str, AfterValidator(validate_name)]
Group = Annotated[str, AfterValidator(validate_group)]
Hostname = Annotated[str, AfterValidator(validate_hostname)]
Branch = Annotated[str, AfterValidator(validate_branch)]
