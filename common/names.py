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

import hashlib
import re
from typing import Annotated
from urllib.parse import urlsplit

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
_UNDERSCORE = str.maketrans({"_": "-"})

# Characters an OCI tag may not contain; the tag must also start alphanumeric
# or '_' and is capped at 128 characters.
_TAG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_TAG_MAX = 128

# A KSVC name and a DNS label are both capped here, and {name}-{group} is both.
MAX_OBJECT_NAME = 63


def normalize_group(group: str) -> str:
    """Normalize a group name to its bare, DNS-safe form.

    Strips the Keycloak path prefix ("/"), lowercases, strips a leading
    ``ggd-<1-4 digits>-`` prefix, then folds "_" to "-". So "/ggd-1234-platforms",
    "Platforms" and "platforms" - or "My_Team" and "my-team" - each name the same
    group. Applied both to groups from the OIDC token and to a request-supplied
    group, so the two always agree and membership checks compare like with like.

    Lowercasing runs *before* the prefix strip so an upper-case prefix
    ("GGD-1234-Team") is still recognized, and before the "_" fold so neither
    rule can mask the other.

    Args:
        group: The raw group name.

    Returns:
        The normalized group name.
    """
    return _GGD_PREFIX.sub("", group.lstrip("/").lower()).translate(_UNDERSCORE)


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

    Normalization runs first, so a ``ggd-<digits>-`` prefix, "_" separators and
    upper case are accepted on input; the check applies to the normalized form.

    Args:
        group: The candidate group name.

    Returns:
        The normalized group name.

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

    The platform authenticates a clone with a basic-auth Secret (a username and
    the caller's token), which only applies over http(s). An scp-style ref like
    ``git@host:org/repo.git`` has no scheme, so it silently becomes a
    ``kpack.io/git`` annotation kpack cannot match the credential against, and
    the build fails as an auth error nowhere near its cause. An empty URL is
    accepted by every check downstream and fails the same way.

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
        raise ValueError(
            "gitRepo must not embed credentials; send the token as gitToken instead"
        )
    return url.strip()


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


def object_name(name: str, group: str) -> str:
    """The cluster name of a workload and everything derived from it.

    ``{name}-{group}`` is the platform's primary key: the KSVC, its config
    Secrets, its git Secret and its kpack Image all hang off it, and a service
    that starts from an Image has to be able to get back to the KSVC. It lives
    here so the rule is written once - it was previously a helper in the
    workloads engine plus two inline copies in the builder.

    Args:
        name: The workload name.
        group: The owning group.

    Returns:
        The derived object name.
    """
    return f"{name}-{group}"


def validate_object_name(name: str, group: str) -> str:
    """Check that ``{name}-{group}`` still fits where it has to be written.

    ``name`` and ``group`` are each a legal DNS-1123 label, but the primary key
    is their concatenation and that is what becomes the KSVC name and the first
    label of ``{name}-{group}.{route_domain}``. Both are capped at 63: Knative
    backs a Service with a Kubernetes Service, and a DNS label cannot be longer.
    Two 63-character halves are individually valid and produce a 127-character
    name the cluster rejects, so the pair has to be checked as a pair.

    Args:
        name: The workload name.
        group: The owning group.

    Returns:
        The derived object name.

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

    A git branch may contain ``/``; an OCI tag may not, and must start with an
    alphanumeric or ``_``. So the tag is a *projection* of the branch, not the
    branch itself - ``feature/login`` builds from that exact ref but pushes to
    ``feature-login``.

    Two branches differing only in characters that get replaced would collide on
    one tag (``feature/login`` and ``feature-login``). That is accepted: the
    alternative is an encoded tag nobody can read in a registry listing, and the
    pair is unusual in one repository. The git revision is never rewritten, so a
    build always compiles the branch that was asked for.

    A branch can also project to *nothing*: git refs are UTF-8, so a branch with
    no ASCII in it at all is legal and every character of it is replaced. An
    empty tag would silently produce the malformed reference ``repo:`` on both
    the kpack Image and the KSVC, so those fall back to a digest of the branch -
    unreadable, but deterministic, which is what active/active convergence needs.

    Args:
        branch: The branch name (already validated).

    Returns:
        The tag to push to, never empty.
    """
    tag = _TAG_UNSAFE.sub("-", branch).lstrip(".-")[:_TAG_MAX]
    if not tag:
        return "b-" + hashlib.sha256(branch.encode()).hexdigest()[:12]
    return tag


# Validated string types shared by request models, query params and the build
# contract. The group validator also NORMALIZES ("/ggd-1234-team" -> "team",
# "My_Team" -> "my-team"), so every group entering the app is already in bare,
# canonical form at the edge - nothing downstream re-normalizes.
Name = Annotated[str, AfterValidator(validate_name)]
Group = Annotated[str, AfterValidator(validate_group)]
Hostname = Annotated[str, AfterValidator(validate_hostname)]
Branch = Annotated[str, AfterValidator(validate_branch)]
GitUrl = Annotated[str, AfterValidator(validate_git_url)]
SourcePath = Annotated[str, AfterValidator(validate_source_path)]
