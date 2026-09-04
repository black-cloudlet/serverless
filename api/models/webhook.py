"""The git-webhook payload, and what the API answers a delivery with.

A push reaches the API as ``POST .../functions/{name}/build`` carrying
``X-Gitlab-Token`` instead of a bearer (docs/FUNCTIONS.md - Git webhook). Only
what the build/ignore decision needs is modelled, so a payload change upstream
cannot fail a delivery.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

# GitLab names the event kind in a header and repeats it in the body; both are
# checked, because a hook set to send everything is a routine mistake.
GITLAB_PUSH_EVENT = "Push Hook"
GITLAB_PUSH_OBJECT_KIND = "push"

_BRANCH_REF = "refs/heads/"
# SHA-1, or SHA-256 in a migrated repository. Both accepted, but it must be a
# hex object id - not a ref name smuggled in as one.
_SHA = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")


class GitLabProject(BaseModel):
    """The repository half of a push event."""

    model_config = ConfigDict(extra="ignore")

    git_http_url: str = ""


class GitLabPushEvent(BaseModel):
    """One GitLab push event, reduced to what decides whether to build.

    Lenient (``extra="ignore"``). A field this does read that is absent or
    malformed makes the push *ignorable*, not an error - see the properties.
    """

    model_config = ConfigDict(extra="ignore")

    object_kind: str = ""
    ref: str = ""
    before: str = ""
    after: str = ""
    # The commit to build. `after` is the same in the ordinary case, and the
    # fallback when GitLab omits this.
    checkout_sha: str | None = None
    project: GitLabProject = Field(default_factory=GitLabProject)

    @property
    def is_push(self) -> bool:
        """Whether the body says this is a push (the header is checked separately)."""
        return self.object_kind == GITLAB_PUSH_OBJECT_KIND

    @property
    def branch(self) -> str | None:
        """The branch this push updated, or None if the ref is not a branch.

        A tag push names no branch, so it matches no revision and is ignored.
        """
        if not self.ref.startswith(_BRANCH_REF):
            return None
        return self.ref[len(_BRANCH_REF) :] or None

    @property
    def is_deletion(self) -> bool:
        """Whether this push deleted the ref.

        Git marks a deletion with the all-zero object id, which is no commit.
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
    an unwanted push must read as success. ``reason`` is what a human reads in
    the delivery log.
    """

    accepted: bool = False
    reason: str | None = None
