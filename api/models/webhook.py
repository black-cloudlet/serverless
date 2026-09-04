"""The git-webhook payload, and what the API answers a delivery with.

A push reaches the API as ``POST .../functions/{name}/build`` carrying
``X-Gitlab-Token`` instead of a bearer (docs/FUNCTIONS.md - Git webhook). This
module models only what that decision needs off the payload; everything else
GitLab sends is ignored, so a payload change upstream cannot fail a delivery.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

# GitLab names the event kind in a header and repeats it in the body; both are
# checked, because a hook misconfigured to send everything is a routine mistake.
GITLAB_PUSH_EVENT = "Push Hook"
GITLAB_PUSH_OBJECT_KIND = "push"

_BRANCH_REF = "refs/heads/"
# SHA-1 today, SHA-256 in a repository that has migrated. Both are accepted:
# the value is written into a git revision, so its shape is git's business, but
# it must be a hex object id and not a ref name smuggled in as one.
_SHA = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")


class GitLabProject(BaseModel):
    """The repository half of a push event."""

    model_config = ConfigDict(extra="ignore")

    git_http_url: str = ""


class GitLabPushEvent(BaseModel):
    """One GitLab push event, reduced to what decides whether to build.

    Lenient by construction (``extra="ignore"``): GitLab sends commits, the
    pusher, project metadata and more, none of which this platform reads. A
    field it does read that is absent or malformed makes the push *ignorable*,
    not an error - see :meth:`branch` and :meth:`sha`.
    """

    model_config = ConfigDict(extra="ignore")

    object_kind: str = ""
    ref: str = ""
    before: str = ""
    after: str = ""
    # Set on a push; the commit the build should use. `after` is the same value
    # in the ordinary case, and is the fallback when GitLab omits this.
    checkout_sha: str | None = None
    project: GitLabProject = Field(default_factory=GitLabProject)

    @property
    def is_push(self) -> bool:
        """Whether the body says this is a push (the header is checked separately)."""
        return self.object_kind == GITLAB_PUSH_OBJECT_KIND

    @property
    def branch(self) -> str | None:
        """The branch this push updated, or None if the ref is not a branch.

        A tag push carries ``refs/tags/...`` and anything else carries whatever
        the provider chose; neither names a branch, so neither can match a
        function's revision and both are ignored.
        """
        if not self.ref.startswith(_BRANCH_REF):
            return None
        return self.ref[len(_BRANCH_REF) :] or None

    @property
    def is_deletion(self) -> bool:
        """Whether this push deleted the ref.

        Git marks a deletion by pushing the all-zero object id, which is not a
        commit anything can be built from.
        """
        return bool(self.after) and set(self.after) == {"0"}

    @property
    def sha(self) -> str | None:
        """The commit to build, or None when the payload names no usable one."""
        candidate = self.checkout_sha or self.after
        if not candidate or not _SHA.fullmatch(candidate):
            return None
        return candidate


class WebhookOutcome(BaseModel):
    """The answer to a delivery that started no build.

    A `200`, not an error: GitLab disables a hook that keeps returning `4xx`, so
    a push this function does not care about - another branch, a tag, a deleted
    ref - has to read as success. ``reason`` is for the delivery log a human
    reads when a push did not do what they expected.
    """

    accepted: bool = False
    reason: str | None = None
