"""Async Linear GraphQL client using OAuth client-credentials app tokens.

Self-contained (aiohttp + stdlib only; no Hermes imports). Never logs or
embeds tokens/secrets in exceptions.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import aiohttp

from .constants import TOKEN_RENEW_MARGIN_S, UPLOAD_PUT_TIMEOUT_S, WRITE_RETRIES

log = logging.getLogger(__name__)

SCOPES = "read,write,app:mentionable,app:assignable"
_HTTP_TIMEOUT_S = 30
_MAX_RETRY_AFTER_S = 60.0
_DEFAULT_RETRY_AFTER_S = 1.0
_AUTH_CODES = {"AUTHENTICATION_ERROR", "UNAUTHENTICATED"}
_RATELIMIT_CODE = "RATELIMITED"
#: Linear answers an unknown entity with a GraphQL error (``INPUT_ERROR``, HTTP 200)
#: whose message starts with this (live probe); the client reports it as ``NOT_FOUND``.
_NOT_FOUND_PREFIX = "Entity not found"
_PAGE_SIZE = 250
#: Upper bound on nodes read by a paged query (comments, labels).
PAGE_CAP = 2000

# -- GraphQL documents (verified against the Linear SDL) ---------------

QUERY_VIEWER = """query Viewer { viewer { id name displayName } }"""

QUERY_USER = """query User($id: String!) { user(id: $id) { email name } }"""

MUTATION_ACTIVITY = """mutation AgentActivityCreate($input: AgentActivityCreateInput!) {
  agentActivityCreate(input: $input) { success agentActivity { id } }
}"""

MUTATION_SESSION_ON_ISSUE = """mutation AgentSessionCreateOnIssue($input: AgentSessionCreateOnIssue!) {
  agentSessionCreateOnIssue(input: $input) { success agentSession { id } }
}"""

MUTATION_SESSION_UPDATE = """mutation AgentSessionUpdate($id: String!, $input: AgentSessionUpdateInput!) {
  agentSessionUpdate(id: $id, input: $input) { success }
}"""

# Query.agentSessions has no issue/appUser filter in the SDL (only paging +
# orderBy) and Issue exposes no agentSessions connection, so we fetch the
# newest page and filter client-side.
QUERY_ISSUE_APP_SESSIONS = """query AppSessions {
  agentSessions(first: 250, orderBy: createdAt) {
    nodes { id createdAt dismissedAt status appUser { id } issue { id identifier } }
  }
}"""

QUERY_ISSUE_SNAPSHOT = """query IssueSnapshot($id: String!) {
  issue(id: $id) {
    identifier title description
    state { name type }
    labels { nodes { id name } }
    assignee { name }
    project { id name }
    team { id }
  }
}"""

QUERY_TEAM_STARTED = """query TeamStartedStates($teamId: String!) {
  team(id: $teamId) {
    states(filter: { type: { eq: "started" } }) { nodes { id name position } }
  }
}"""

MUTATION_ISSUE_UPDATE = """mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success issue { id identifier title url } }
}"""

# -- agent tool operations (RET-76) ---------------------------------------

QUERY_ISSUE_GET = """query IssueGet($id: String!) {
  issue(id: $id) {
    id identifier title description url subIssueSortOrder
    creator { id }
    state { id name type }
    team { id }
    project { id name externalLinks { nodes { label url } } }
    parent { id identifier }
    labels { nodes { id name } }
    children(first: 250) {
      nodes { id identifier title subIssueSortOrder createdAt state { type } creator { id } }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""

# Further pages of ``Issue.children`` (same node fields as ``QUERY_ISSUE_GET``).
QUERY_ISSUE_CHILDREN = """query IssueChildren($id: String!, $after: String) {
  issue(id: $id) {
    children(first: 250, after: $after) {
      nodes { id identifier title subIssueSortOrder createdAt state { type } creator { id } }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""

# ``linear_issue_get``'s extra fields (RET-120 §2.2 step 2); ``issue_get`` stays as it is.
QUERY_ISSUE_DETAILS = """query IssueDetails($id: String!) {
  issue(id: $id) {
    assignee { id name } delegate { id name }
    priority priorityLabel estimate dueDate
    cycle { id number name } projectMilestone { id name }
    relations(first: 50) { nodes { type relatedIssue { identifier title state { name } } } }
    inverseRelations(first: 50) { nodes { type issue { identifier title state { name } } } }
    attachments(first: 50) { nodes { id title subtitle url sourceType } }
    documents { nodes { id title url } }
  }
}"""

# Top-level comments with up to 50 replies each (RET-120 §2.2 step 3). Linear's createdAt
# order is newest first (live probe), so ``first`` takes the newest threads (Ruling R1).
QUERY_ISSUE_THREADS = """query IssueThreads($id: String!, $first: Int!) {
  issue(id: $id) {
    comments(first: $first, orderBy: createdAt, filter: { parent: { null: true } }) {
      nodes {
        id url body createdAt resolvedAt user { id name }
        children(first: 50, orderBy: createdAt) {
          nodes { id body createdAt resolvedAt user { id name } }
        }
      }
    }
  }
}"""

# ``linear_search`` rows (RET-120 §1.2): the node fields both queries return.
_SEARCH_NODE = ("identifier title url updatedAt priority state { name } assignee { name } "
                "delegate { name } project { name }")

QUERY_SEARCH_ISSUES = """query SearchIssues($term: String!, $filter: IssueFilter,
    $includeComments: Boolean, $first: Int!, $after: String) {
  searchIssues(term: $term, filter: $filter, includeComments: $includeComments,
               first: $first, after: $after) {
    nodes { %s }
    pageInfo { hasNextPage endCursor }
  }
}""" % _SEARCH_NODE

QUERY_LIST_ISSUES = """query ListIssues($filter: IssueFilter, $first: Int!, $after: String) {
  issues(filter: $filter, first: $first, after: $after, orderBy: updatedAt) {
    nodes { %s }
    pageInfo { hasNextPage endCursor }
  }
}""" % _SEARCH_NODE

MUTATION_ISSUE_CREATE = """mutation IssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) { success issue { id identifier title url } }
}"""

MUTATION_COMMENT_CREATE = """mutation CommentCreate($input: CommentCreateInput!) {
  commentCreate(input: $input) { success comment { id } }
}"""

MUTATION_COMMENT_UPDATE = """mutation CommentUpdate($id: String!, $input: CommentUpdateInput!) {
  commentUpdate(id: $id, input: $input) { success comment { id } }
}"""

# A comment's issue, thread parent and resolution (RET-121 §1.5). Linear refuses a reply's
# id as ``parentId`` (live probe RET-142), so a thread is one level deep.
QUERY_COMMENT_GET = """query CommentGet($id: String!) {
  comment(id: $id) { id resolvedAt issue { id } parent { id } }
}"""

MUTATION_COMMENT_RESOLVE = """mutation CommentResolve($id: String!, $resolvingCommentId: String) {
  commentResolve(id: $id, resolvingCommentId: $resolvingCommentId) { success comment { id } }
}"""

# Server-side filter on the app user; ``user.id`` is re-checked client-side. Paged.
QUERY_ISSUE_APP_COMMENTS = """query IssueAppComments($id: String!, $userId: ID!, $after: String) {
  issue(id: $id) {
    comments(first: 250, after: $after, filter: { user: { id: { eq: $userId } } }) {
      nodes { id body createdAt user { id } }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""

QUERY_TEAM_STATES = """query TeamStates($teamId: String!) {
  team(id: $teamId) { states(first: 250) { nodes { id name type position } } }
}"""

# Workspace teams visible to the app (root connection; the app user is usually not a
# team member, so viewer.teams is empty). Paged.
QUERY_TEAMS = """query Teams($after: String) {
  teams(first: 250, after: $after) {
    nodes { id key name }
    pageInfo { hasNextPage endCursor }
  }
}"""

# Team labels plus workspace labels (no team); group labels cannot be applied. Paged.
QUERY_TEAM_LABELS = """query TeamLabels($teamId: ID!, $after: String) {
  issueLabels(first: 250, after: $after,
              filter: { or: [{ team: { id: { eq: $teamId } } }, { team: { null: true } }] }) {
    nodes { id name isGroup }
    pageInfo { hasNextPage endCursor }
  }
}"""

# Name directory lists (RET-121 spec §2.1). Paged like QUERY_TEAMS.
QUERY_USERS = """query Users($after: String) {
  users(first: 250, after: $after) {
    nodes { id name displayName email app active }
    pageInfo { hasNextPage endCursor }
  }
}"""

QUERY_TEAM_CYCLES = """query TeamCycles($teamId: String!, $after: String) {
  team(id: $teamId) {
    activeCycle { id }
    cycles(first: 250, after: $after) {
      nodes { id number name startsAt endsAt }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""

QUERY_PROJECTS = """query Projects($after: String) {
  projects(first: 250, after: $after) {
    nodes { id name state }
    pageInfo { hasNextPage endCursor }
  }
}"""

QUERY_PROJECT_MILESTONES = """query ProjectMilestones($projectId: String!, $after: String) {
  project(id: $projectId) {
    projectMilestones(first: 250, after: $after) {
      nodes { id name }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""

# Both directions of an issue's relations (RET-121 spec §2.1), first 250 each.
QUERY_ISSUE_RELATIONS = """query IssueRelations($id: String!) {
  issue(id: $id) {
    relations(first: 250) { nodes { id type issue { id } relatedIssue { id } } }
    inverseRelations(first: 250) { nodes { id type issue { id } relatedIssue { id } } }
  }
}"""

# Live probe (RET-141, 2026-11-24): Linear keeps one relation per issue pair. An identical
# create answers the existing relation (same id); another type or direction replaces it.
# ``duplicate``: ``issueId`` is the duplicate (it moves to the Duplicate state).
MUTATION_RELATION_CREATE = """mutation IssueRelationCreate($input: IssueRelationCreateInput!) {
  issueRelationCreate(input: $input) { success issueRelation { id } }
}"""

MUTATION_FILE_UPLOAD = """mutation FileUpload($size: Int!, $filename: String!, $contentType: String!) {
  fileUpload(size: $size, filename: $filename, contentType: $contentType) {
    success uploadFile { uploadUrl assetUrl headers { key value } }
  }
}"""

MUTATION_ATTACHMENT_CREATE = """mutation AttachmentCreate($input: AttachmentCreateInput!) {
  attachmentCreate(input: $input) { success attachment { id url } }
}"""

# Re-linking a URL already on the issue is an INPUT_ERROR (live probe 2026-11-24):
# neither mutation updates or duplicates the existing attachment.
MUTATION_ATTACHMENT_LINK_URL = """mutation AttachmentLinkURL($issueId: String!, $url: String!,
    $title: String) {
  attachmentLinkURL(issueId: $issueId, url: $url, title: $title) {
    success attachment { id url }
  }
}"""

MUTATION_ATTACHMENT_LINK_GITHUB_PR = """mutation AttachmentLinkGitHubPR($issueId: String!,
    $url: String!) {
  attachmentLinkGitHubPR(issueId: $issueId, url: $url) { success attachment { id url } }
}"""

#: ``Cache-Control`` of every upload PUT (with ``Content-Type`` and the ``uploadFile.headers``).
_UPLOAD_CACHE_CONTROL = "public, max-age=31536000"

# -- documents and projects (RET-122 spec §1; live probes 2026-11-25) ------

# ``id`` is a UUID or a ``slugId``; an unknown one is ``Entity not found: Document``.
QUERY_DOCUMENT_GET = """query DocumentGet($id: String!) {
  document(id: $id) {
    id title url content updatedAt
    creator { name }
    issue { id identifier title }
    project { id name }
  }
}"""

QUERY_ISSUE_DOCUMENTS = """query IssueDocuments($id: String!) {
  issue(id: $id) { documents(first: 250) { nodes { id title url creator { id } } } }
}"""

QUERY_PROJECT_DOCUMENTS = """query ProjectDocuments($id: String!) {
  project(id: $id) { documents(first: 250) { nodes { id title url creator { id } } } }
}"""

MUTATION_DOCUMENT_CREATE = """mutation DocumentCreate($input: DocumentCreateInput!) {
  documentCreate(input: $input) { success document { id url } }
}"""

# The input also takes issueId, projectId, trashed and hiddenAt; ``document_update`` never
# sends them (spec §1.2).
MUTATION_DOCUMENT_UPDATE = """mutation DocumentUpdate($id: String!, $input: DocumentUpdateInput!) {
  documentUpdate(id: $id, input: $input) { success document { id url } }
}"""

# Linear refuses ``first: 0`` ("first must not be less than 1", live probe), so ``updates``
# 0 drops the connection with ``@include`` instead.
QUERY_PROJECT_DETAIL = """query ProjectDetail($id: String!, $updates: Int!, $withUpdates: Boolean!) {
  project(id: $id) {
    id name url health targetDate
    status { name }
    lead { name }
    externalLinks(first: 250) { nodes { id label url } }
    projectMilestones(first: 250) { nodes { id name targetDate } }
    documents(first: 250) { nodes { id title url } }
    projectUpdates(first: $updates, orderBy: createdAt) @include(if: $withUpdates) {
      nodes { id createdAt health body user { name } }
    }
  }
}"""

MUTATION_PROJECT_UPDATE_CREATE = """mutation ProjectUpdateCreate($input: ProjectUpdateCreateInput!) {
  projectUpdateCreate(input: $input) { success projectUpdate { id url } }
}"""

# A duplicate name is Linear's INPUT_ERROR "name not unique" (userPresentableMessage
# "A project milestone called '<name>' already exists on this project.").
MUTATION_MILESTONE_CREATE = """mutation ProjectMilestoneCreate($input: ProjectMilestoneCreateInput!) {
  projectMilestoneCreate(input: $input) { success projectMilestone { id name targetDate } }
}"""

MUTATION_MILESTONE_UPDATE = """mutation ProjectMilestoneUpdate($id: String!,
    $input: ProjectMilestoneUpdateInput!) {
  projectMilestoneUpdate(id: $id, input: $input) {
    success projectMilestone { id name targetDate }
  }
}"""

# Linear does not refuse a duplicate URL (brainstorm probe). ``label`` is non-null in the
# input (introspection). The input also takes initiativeId / releaseId; never sent.
MUTATION_PROJECT_LINK_CREATE = """mutation EntityExternalLinkCreate(
    $input: EntityExternalLinkCreateInput!) {
  entityExternalLinkCreate(input: $input) { success entityExternalLink { id } }
}"""

#: The only ``DocumentUpdateInput`` / ``ProjectMilestoneUpdateInput`` fields the client sends.
DOCUMENT_UPDATE_FIELDS = frozenset({"title", "content"})
MILESTONE_UPDATE_FIELDS = frozenset({"name", "description", "targetDate"})


class LinearError(Exception):
    """A Linear API failure. ``code`` is the GraphQL/OAuth error code if any; ``detail`` is
    Linear's ``extensions.userPresentableMessage`` if any."""

    def __init__(self, message: str = "", *, code: str | None = None, status: int | None = None,
                 detail: str | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"LinearError({str(self)!r}, code={self.code!r}, status={self.status!r}, "
                f"detail={self.detail!r})")


class UploadError(LinearError):
    """The upload PUT answered a non-2xx ``status`` (``code`` is ``UPLOAD_FAILED``)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"upload failed (HTTP {status})", code="UPLOAD_FAILED", status=status)


def _first_error(body: Any) -> tuple[str | None, str, str | None]:
    """``(code, message, detail)`` of the first GraphQL error (``detail``: the
    ``extensions.userPresentableMessage``)."""
    errors = body.get("errors") if isinstance(body, dict) else None
    if not errors:
        return None, "", None
    err = errors[0] if isinstance(errors[0], dict) else {}
    ext = err.get("extensions") if isinstance(err.get("extensions"), dict) else {}
    detail = ext.get("userPresentableMessage")
    return (ext.get("code"), str(err.get("message") or "GraphQL error"),
            str(detail) if detail else None)


def _retry_after(headers: Any) -> float:
    raw = headers.get("Retry-After") if headers is not None else None
    if raw is None:
        return _DEFAULT_RETRY_AFTER_S
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_RETRY_AFTER_S
    return min(max(val, 0.0), _MAX_RETRY_AFTER_S)


class LinearClient:
    def __init__(
        self,
        client_id: str,
        client_secret: Callable[[], str],
        *,
        base_url: str = "https://api.linear.app",
        session: aiohttp.ClientSession | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._base = base_url.rstrip("/")
        self._session = session
        self._own_session = session is None
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._renew_at = 0.0
        self._viewer_id: str | None = None
        #: Wall-clock expiry of the cached token (status display only; renewal uses ``clock``).
        self.token_expires_at: float | None = None

    # -- plumbing -------------------------------------------------------

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or (self._own_session and self._session.closed):
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
            )
            self._own_session = True
        return self._session

    async def close(self) -> None:
        if self._own_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def _fetch_token(self) -> None:
        """Request a new client-credentials token. Caller holds ``_lock``."""
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret()}".encode()).decode()
        try:
            async with self._http().post(
                f"{self._base}/oauth/token",
                data={"grant_type": "client_credentials", "scope": SCOPES},
                headers={"Authorization": f"Basic {basic}"},
            ) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    body = None
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise LinearError(f"token request failed: {type(exc).__name__}", code="NETWORK") from None
        if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
            code = body.get("error") if isinstance(body, dict) else None
            raise LinearError(f"token request failed (HTTP {status})", code=code, status=status)
        expires_in = float(body.get("expires_in") or 0)
        self._token = body["access_token"]
        self._renew_at = self._clock() + expires_in - TOKEN_RENEW_MARGIN_S
        self.token_expires_at = time.time() + expires_in if expires_in else None
        log.debug("linear: obtained app token (expires_in=%s)", int(expires_in))

    def _token_fresh(self) -> bool:
        return self._token is not None and self._clock() < self._renew_at

    async def token(self) -> str:
        if self._token_fresh():
            return self._token  # type: ignore[return-value]
        async with self._lock:
            if not self._token_fresh():
                await self._fetch_token()
            return self._token  # type: ignore[return-value]

    async def _renew_after_401(self, rejected: str) -> None:
        """Renew once per 401 burst: only if the cached token is still ``rejected``."""
        async with self._lock:
            if self._token == rejected:
                self._token = None
                await self._fetch_token()

    async def _post_gql(self, token: str, payload: dict) -> tuple[int, Any, Any]:
        try:
            async with self._http().post(
                f"{self._base}/graphql",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    body = None
                return resp.status, body, resp.headers
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise LinearError(f"GraphQL request failed: {type(exc).__name__}", code="NETWORK") from None

    async def gql(self, query: str, variables: dict | None = None) -> dict:
        payload = {"query": query, "variables": variables or {}}
        renewed = False
        rate_retries = 0
        while True:
            tok = await self.token()
            status, body, headers = await self._post_gql(tok, payload)
            code, message, detail = _first_error(body)

            if status == 401 or code in _AUTH_CODES:
                if renewed:
                    raise LinearError(message or "unauthorized", code=code or "UNAUTHENTICATED",
                                      status=status)
                renewed = True
                await self._renew_after_401(tok)
                continue

            if status == 429 or code == _RATELIMIT_CODE:
                if rate_retries >= WRITE_RETRIES:
                    raise LinearError(message or "rate limited", code=_RATELIMIT_CODE, status=429)
                rate_retries += 1
                await self._sleep(_retry_after(headers))
                continue

            if code is not None or message:
                if message.startswith(_NOT_FOUND_PREFIX):
                    code = "NOT_FOUND"  # Linear's INPUT_ERROR for an unknown entity
                raise LinearError(message, code=code, status=status, detail=detail)
            if status >= 400 or not isinstance(body, dict) or not isinstance(body.get("data"), dict):
                raise LinearError(f"unexpected GraphQL response (HTTP {status})", status=status)
            return body["data"]

    # -- API ------------------------------------------------------------

    async def viewer(self) -> dict:
        """The app user: ``{"id", "name", "displayName"}`` (displayName is the @handle)."""
        data = await self.gql(QUERY_VIEWER)
        v = data["viewer"]
        self._viewer_id = str(v["id"])
        return {"id": self._viewer_id, "name": v.get("name"), "displayName": v.get("displayName")}

    async def viewer_id(self) -> str:
        if self._viewer_id is None:
            await self.viewer()
        return self._viewer_id  # type: ignore[return-value]

    async def user(self, user_id: str) -> tuple[str, str]:
        data = await self.gql(QUERY_USER, {"id": user_id})
        u = data["user"]
        return u.get("email") or "", u.get("name") or ""

    @staticmethod
    def _require_success(data: dict, field: str) -> dict:
        result = data.get(field) or {}
        if not result.get("success"):
            raise LinearError(f"{field} unsuccessful")
        return result

    async def create_activity(
        self,
        agent_session_id: str,
        content: dict,
        *,
        ephemeral: bool = False,
        signal: str | None = None,
        signal_metadata: dict | None = None,
    ) -> str:
        inp: dict[str, Any] = {
            "agentSessionId": agent_session_id,
            "content": content,
            "ephemeral": ephemeral,
        }
        if signal is not None:
            inp["signal"] = signal
        if signal_metadata is not None:
            inp["signalMetadata"] = signal_metadata
        data = await self.gql(MUTATION_ACTIVITY, {"input": inp})
        result = self._require_success(data, "agentActivityCreate")
        return result["agentActivity"]["id"]

    async def create_session_on_issue(self, issue_id: str) -> str:
        data = await self.gql(MUTATION_SESSION_ON_ISSUE, {"input": {"issueId": issue_id}})
        return self._require_success(data, "agentSessionCreateOnIssue")["agentSession"]["id"]

    async def app_sessions_on_issue(self, issue_id: str) -> list[dict]:
        me = await self.viewer_id()
        data = await self.gql(QUERY_ISSUE_APP_SESSIONS)
        nodes = (data.get("agentSessions") or {}).get("nodes") or []
        mine = [
            n for n in nodes
            if (n.get("appUser") or {}).get("id") == me
            and n.get("dismissedAt") is None
            and issue_id in ((n.get("issue") or {}).get("id"), (n.get("issue") or {}).get("identifier"))
        ]
        mine.sort(key=lambda n: n.get("createdAt") or "", reverse=True)
        return mine

    async def update_session(
        self,
        agent_session_id: str,
        *,
        plan: list[dict] | None = None,
        external_urls: list[dict] | None = None,
        added_external_urls: list[dict] | None = None,
    ) -> None:
        """``external_urls`` replaces the whole link list; ``added_external_urls`` appends
        (SDL ``AgentSessionUpdateInput``: the two cannot be combined)."""
        if external_urls is not None and added_external_urls is not None:
            raise ValueError("external_urls and added_external_urls cannot be combined")
        inp: dict[str, Any] = {}
        if plan is not None:
            inp["plan"] = plan
        if external_urls is not None:
            inp["externalUrls"] = external_urls
        if added_external_urls is not None:
            inp["addedExternalUrls"] = added_external_urls
        if not inp:
            return
        data = await self.gql(MUTATION_SESSION_UPDATE, {"id": agent_session_id, "input": inp})
        self._require_success(data, "agentSessionUpdate")

    async def issue_snapshot(self, issue_id: str) -> dict:
        data = await self.gql(QUERY_ISSUE_SNAPSHOT, {"id": issue_id})
        i = data["issue"]
        state = i.get("state") or {}
        project = i.get("project")
        return {
            "identifier": i["identifier"],
            "title": i.get("title") or "",
            "state": {"name": state.get("name"), "type": state.get("type")},
            "labels": [n["name"] for n in ((i.get("labels") or {}).get("nodes") or [])],
            "assignee": (i.get("assignee") or {}).get("name"),
            "project": {"name": project.get("name"), "id": project.get("id")} if project else None,
            "team": {"id": (i.get("team") or {}).get("id")},
            "description": i.get("description") or "",
        }

    async def move_to_first_started(self, issue_id: str, team_id: str) -> bool:
        data = await self.gql(QUERY_TEAM_STARTED, {"teamId": team_id})
        states = (((data.get("team") or {}).get("states") or {}).get("nodes")) or []
        if not states:
            return False
        first = min(states, key=lambda s: s.get("position", 0))
        upd = await self.gql(MUTATION_ISSUE_UPDATE, {"id": issue_id, "input": {"stateId": first["id"]}})
        self._require_success(upd, "issueUpdate")
        return True

    # -- agent tool operations (RET-76) -----------------------------------

    @staticmethod
    def _nodes(conn: Any) -> list[dict]:
        return list((conn or {}).get("nodes") or []) if isinstance(conn, dict) else []

    async def _paged(self, query: str, variables: dict,
                     conn_of: Callable[[dict], Any], *, cap: int = PAGE_CAP,
                     after: str | None = None) -> list[dict]:
        """All nodes of a cursor-paged connection (``pageInfo{hasNextPage endCursor}``),
        at most ``cap``, starting after cursor ``after`` (``None``: the first page)."""
        out: list[dict] = []
        while True:
            data = await self.gql(query, {**variables, "after": after})
            conn = conn_of(data)
            out.extend(self._nodes(conn))
            info = (conn or {}).get("pageInfo") if isinstance(conn, dict) else None
            after = (info or {}).get("endCursor")
            if not (info or {}).get("hasNextPage") or not after:
                return out
            if len(out) >= cap:
                log.warning("linear: paged read stopped at %d nodes (cap)", len(out))
                return out[:cap]

    @staticmethod
    def _child_order(c: dict) -> tuple:
        """``subIssueSortOrder`` ascending (unset last), then ``createdAt``, identifier."""
        sso = c.get("subIssueSortOrder")
        return (sso is None, sso if isinstance(sso, (int, float)) else 0.0,
                c.get("createdAt") or "", c.get("identifier") or "")

    async def issue_get(self, issue_id: str) -> dict:
        """An issue by UUID or identifier, connections flattened to lists
        (``labels``, ``children``, ``project.externalLinks``). Null issue: ``NOT_FOUND``.

        ``children`` is every page (up to ``PAGE_CAP``); each child carries ``creator``
        (``{id}`` or ``None``) so callers can tell the app's children from humans'."""
        data = await self.gql(QUERY_ISSUE_GET, {"id": issue_id})
        i = data.get("issue")
        if not isinstance(i, dict) or not i.get("id"):
            raise LinearError("issue not found", code="NOT_FOUND")
        out = dict(i)
        out["labels"] = self._nodes(i.get("labels"))
        kids = self._nodes(i.get("children"))
        info = (i.get("children") or {}).get("pageInfo") if isinstance(i.get("children"),
                                                                       dict) else None
        if (info or {}).get("hasNextPage") and (info or {}).get("endCursor") \
                and len(kids) < PAGE_CAP:
            kids += await self._paged(
                QUERY_ISSUE_CHILDREN, {"id": i["id"]},
                lambda d: (d.get("issue") or {}).get("children"),
                cap=PAGE_CAP - len(kids), after=info["endCursor"])
        # Linear returns children in no particular order (live probe): sort them.
        out["children"] = sorted(kids, key=self._child_order)
        if isinstance(i.get("project"), dict):
            out["project"] = {**i["project"],
                              "externalLinks": self._nodes(i["project"].get("externalLinks"))}
        return out

    async def _issue_node(self, query: str, variables: dict) -> dict:
        data = await self.gql(query, variables)
        i = data.get("issue")
        if not isinstance(i, dict):
            raise LinearError("issue not found", code="NOT_FOUND")
        return i

    async def issue_details(self, issue_id: str) -> dict:
        """The raw ``issue`` node of ``QUERY_ISSUE_DETAILS``. Null issue: ``NOT_FOUND``."""
        return await self._issue_node(QUERY_ISSUE_DETAILS, {"id": issue_id})

    async def issue_threads(self, issue_id: str, first: int) -> list[dict]:
        """The raw top-level comment nodes (each with ``children.nodes``), at most ``first``,
        in Linear's order. Null issue: ``NOT_FOUND``."""
        i = await self._issue_node(QUERY_ISSUE_THREADS, {"id": issue_id, "first": first})
        return self._nodes(i.get("comments"))

    @staticmethod
    def _connection(data: dict, key: str) -> dict:
        conn = data.get(key)
        if not isinstance(conn, dict):
            raise LinearError(f"{key} returned no connection")
        return conn

    async def search_issues(self, term: str, filter: dict,  # noqa: A002 - SDL name
                            include_comments: bool, first: int, after: str | None) -> dict:
        """``searchIssues``: the raw connection ``{nodes, pageInfo}``."""
        data = await self.gql(QUERY_SEARCH_ISSUES, {
            "term": term, "filter": filter, "includeComments": include_comments,
            "first": first, "after": after})
        return self._connection(data, "searchIssues")

    async def list_issues(self, filter: dict, first: int,  # noqa: A002 - SDL name
                          after: str | None) -> dict:
        """``issues`` ordered by ``updatedAt`` (newest first): the raw connection."""
        data = await self.gql(QUERY_LIST_ISSUES,
                              {"filter": filter, "first": first, "after": after})
        return self._connection(data, "issues")

    async def issue_create(self, input: dict) -> dict:  # noqa: A002 - SDL argument name
        """``issueCreate``; returns ``{id, identifier, title, url}`` of the new issue."""
        data = await self.gql(MUTATION_ISSUE_CREATE, {"input": input})
        issue = self._require_success(data, "issueCreate").get("issue")
        if not isinstance(issue, dict) or not issue.get("id"):
            raise LinearError("issueCreate returned no issue")
        return issue

    async def issue_update(self, issue_id: str, input: dict) -> dict:  # noqa: A002
        """``issueUpdate``; returns ``{id, identifier, title, url}``."""
        data = await self.gql(MUTATION_ISSUE_UPDATE, {"id": issue_id, "input": input})
        return self._require_success(data, "issueUpdate").get("issue") or {"id": issue_id}

    async def comment_create(self, issue_id: str, body: str,
                             parent_id: str | None = None) -> str:
        """``commentCreate``; with ``parent_id`` (a top-level comment) a thread reply."""
        inp = {"issueId": issue_id, "body": body}
        if parent_id is not None:
            inp["parentId"] = parent_id
        data = await self.gql(MUTATION_COMMENT_CREATE, {"input": inp})
        return self._require_success(data, "commentCreate")["comment"]["id"]

    async def comment_get(self, comment_id: str) -> dict | None:
        """``{id, issue_id, parent_id, resolved_at}``; ``None`` for an unknown comment."""
        try:
            data = await self.gql(QUERY_COMMENT_GET, {"id": comment_id})
        except LinearError as exc:
            if exc.code == "NOT_FOUND":
                return None
            raise
        c = data.get("comment")
        if not isinstance(c, dict) or not c.get("id"):
            return None
        return {"id": c["id"], "issue_id": (c.get("issue") or {}).get("id"),
                "parent_id": (c.get("parent") or {}).get("id"),
                "resolved_at": c.get("resolvedAt")}

    async def comment_resolve(self, comment_id: str, resolving_comment_id: str) -> None:
        """``commentResolve`` of thread ``comment_id`` by the reply ``resolving_comment_id``.
        Linear accepts it on a resolved thread too (re-stamps ``resolvedAt``; probe)."""
        data = await self.gql(MUTATION_COMMENT_RESOLVE, {
            "id": comment_id, "resolvingCommentId": resolving_comment_id})
        self._require_success(data, "commentResolve")

    async def comment_update(self, comment_id: str, body: str) -> None:
        data = await self.gql(MUTATION_COMMENT_UPDATE, {"id": comment_id, "input": {"body": body}})
        self._require_success(data, "commentUpdate")

    async def app_comments(self, issue_id: str) -> list[dict]:
        """``[{id, body}]`` of the comments on ``issue_id`` authored by the app (viewer),
        every page (up to ``PAGE_CAP``), oldest first."""
        me = await self.viewer_id()
        nodes = await self._paged(QUERY_ISSUE_APP_COMMENTS, {"id": issue_id, "userId": me},
                                  lambda d: (d.get("issue") or {}).get("comments"))
        mine = [c for c in nodes if (c.get("user") or {}).get("id") == me]
        mine.sort(key=lambda c: c.get("createdAt") or "")  # stable: ties keep page order
        return [{"id": c["id"], "body": c.get("body") or ""} for c in mine]

    async def team_states(self, team_id: str) -> list[dict]:
        """``[{id, name, type}]`` in workflow position order. Not cached here: the name
        directory caches it (RET-121)."""
        data = await self.gql(QUERY_TEAM_STATES, {"teamId": team_id})
        nodes = self._nodes((data.get("team") or {}).get("states"))
        nodes.sort(key=lambda s: s.get("position") or 0)
        return [{"id": s["id"], "name": s.get("name") or "", "type": s.get("type") or ""}
                for s in nodes]

    async def teams(self) -> list[dict]:
        """``[{id, key, name}]``: the workspace's teams visible to the app."""
        nodes = await self._paged(QUERY_TEAMS, {}, lambda d: d.get("teams"))
        return [{"id": t["id"], "key": t.get("key") or "", "name": t.get("name") or ""}
                for t in nodes]

    async def team_labels(self, team_id: str) -> list[dict]:
        """``[{id, name}]``: the team's and the workspace's labels, groups excluded."""
        nodes = await self._paged(QUERY_TEAM_LABELS, {"teamId": team_id},
                                  lambda d: d.get("issueLabels"))
        return [{"id": lb["id"], "name": lb.get("name") or ""}
                for lb in nodes if not lb.get("isGroup")]

    async def users(self) -> list[dict]:
        """``[{id, name, displayName, email, app, active}]``: the workspace's users."""
        nodes = await self._paged(QUERY_USERS, {}, lambda d: d.get("users"))
        return [{"id": u["id"], "name": u.get("name") or "",
                 "displayName": u.get("displayName") or "", "email": u.get("email") or "",
                 "app": bool(u.get("app")), "active": bool(u.get("active"))} for u in nodes]

    async def cycles(self, team_id: str) -> list[dict]:
        """``[{id, number, name, startsAt, endsAt}]`` of the team, by number. The team's
        ``activeCycle`` carries ``current: True``; the first cycle starting after it (after
        now when the team has none) carries ``next: True``."""
        active: list[str | None] = []

        def conn_of(d: dict) -> Any:
            team = d.get("team") or {}
            if not active:
                active.append((team.get("activeCycle") or {}).get("id"))
            return team.get("cycles")

        nodes = await self._paged(QUERY_TEAM_CYCLES, {"teamId": team_id}, conn_of)
        rows = sorted(({"id": c["id"], "number": c.get("number"), "name": c.get("name") or "",
                        "startsAt": c.get("startsAt") or "", "endsAt": c.get("endsAt") or ""}
                       for c in nodes),
                      key=lambda c: (c["number"] is None, c["number"] or 0))
        current = next((c for c in rows if active and c["id"] == active[0]), None)
        if current is not None:
            current["current"] = True
            after = current["startsAt"]
        else:  # Linear's ISO form: 2026-11-24T08:00:00.000Z, comparable as text
            after = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        upcoming = [c for c in rows if c["startsAt"] > after]
        if upcoming:
            min(upcoming, key=lambda c: c["startsAt"])["next"] = True
        return rows

    async def projects(self) -> list[dict]:
        """``[{id, name, state}]``: the workspace's projects."""
        nodes = await self._paged(QUERY_PROJECTS, {}, lambda d: d.get("projects"))
        return [{"id": p["id"], "name": p.get("name") or "", "state": p.get("state") or ""}
                for p in nodes]

    async def milestones(self, project_id: str) -> list[dict]:
        """``[{id, name}]``: the project's milestones."""
        nodes = await self._paged(QUERY_PROJECT_MILESTONES, {"projectId": project_id},
                                  lambda d: (d.get("project") or {}).get("projectMilestones"))
        return [{"id": m["id"], "name": m.get("name") or ""} for m in nodes]

    # -- uploads and attachments (RET-120) ---------------------------------

    async def issue_relations(self, issue_id: str) -> list[dict]:
        """``[{id, type, issue_id, related_id}]``: the issue's outbound ``relations`` then
        its inbound ``inverseRelations`` (first 250 each)."""
        data = await self.gql(QUERY_ISSUE_RELATIONS, {"id": issue_id})
        issue = data.get("issue") or {}
        return [{"id": r.get("id"), "type": r.get("type"),
                 "issue_id": (r.get("issue") or {}).get("id"),
                 "related_id": (r.get("relatedIssue") or {}).get("id")}
                for key in ("relations", "inverseRelations")
                for r in self._nodes(issue.get(key))]

    async def relation_create(self, issue_id: str, related_id: str, type: str) -> str:  # noqa: A002
        """``issueRelationCreate`` (``issue_id`` <type> ``related_id``); the relation id."""
        data = await self.gql(MUTATION_RELATION_CREATE, {"input": {
            "issueId": issue_id, "relatedIssueId": related_id, "type": type}})
        rel = self._require_success(data, "issueRelationCreate").get("issueRelation")
        if not isinstance(rel, dict) or not rel.get("id"):
            raise LinearError("issueRelationCreate returned no relation")
        return rel["id"]

    async def file_upload(self, name: str, content_type: str, data: bytes) -> str:
        """Upload ``data`` and return its ``assetUrl``: ``fileUpload``, then a PUT of the
        bytes to the signed ``uploadUrl`` with ``Content-Type``, ``Cache-Control`` and every
        returned header, and **no** bearer token. A non-2xx PUT raises ``UploadError``."""
        res = await self.gql(MUTATION_FILE_UPLOAD, {
            "size": len(data), "filename": name, "contentType": content_type})
        up = self._require_success(res, "fileUpload").get("uploadFile")
        if not isinstance(up, dict) or not up.get("uploadUrl") or not up.get("assetUrl"):
            raise LinearError("fileUpload returned no uploadFile")
        headers = {"Content-Type": content_type, "Cache-Control": _UPLOAD_CACHE_CONTROL}
        for h in up.get("headers") or []:
            if isinstance(h, dict) and h.get("key"):
                headers[str(h["key"])] = str(h.get("value") or "")
        try:
            async with self._http().put(
                up["uploadUrl"], data=data, headers=headers,
                timeout=aiohttp.ClientTimeout(total=UPLOAD_PUT_TIMEOUT_S),
            ) as resp:
                status = resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise LinearError(f"upload failed: {type(exc).__name__}", code="NETWORK") from None
        if not 200 <= status < 300:
            raise UploadError(status)
        return str(up["assetUrl"])

    @staticmethod
    def _attachment(data: dict, field: str) -> dict:
        att = LinearClient._require_success(data, field).get("attachment")
        if not isinstance(att, dict) or not att.get("id"):
            raise LinearError(f"{field} returned no attachment")
        return {"id": att["id"], "url": att.get("url")}

    async def attachment_create(self, issue_id: str, title: str, subtitle: str | None,
                                url: str) -> dict:
        """``attachmentCreate``; returns ``{id, url}``. ``subtitle`` is sent only if set."""
        inp: dict[str, Any] = {"issueId": issue_id, "title": title, "url": url}
        if subtitle is not None:
            inp["subtitle"] = subtitle
        data = await self.gql(MUTATION_ATTACHMENT_CREATE, {"input": inp})
        return self._attachment(data, "attachmentCreate")

    async def attachment_link_url(self, issue_id: str, url: str, title: str | None) -> dict:
        """``attachmentLinkURL``; returns ``{id, url}``. A URL already linked to the issue
        is Linear's ``INPUT_ERROR`` (raised as ``LinearError``)."""
        data = await self.gql(MUTATION_ATTACHMENT_LINK_URL,
                              {"issueId": issue_id, "url": url, "title": title})
        return self._attachment(data, "attachmentLinkURL")

    async def attachment_link_github_pr(self, issue_id: str, url: str) -> dict:
        """``attachmentLinkGitHubPR``; returns ``{id, url}``. A PR already linked to the
        issue is Linear's ``INPUT_ERROR`` (raised as ``LinearError``)."""
        data = await self.gql(MUTATION_ATTACHMENT_LINK_GITHUB_PR,
                              {"issueId": issue_id, "url": url})
        return self._attachment(data, "attachmentLinkGitHubPR")

    # -- documents and projects (RET-122) -----------------------------------

    @staticmethod
    def _payload(data: dict, field: str, entity: str) -> dict:
        """``data[field][entity]`` of a successful mutation; a missing id is a LinearError."""
        node = LinearClient._require_success(data, field).get(entity)
        if not isinstance(node, dict) or not node.get("id"):
            raise LinearError(f"{field} returned no {entity}")
        return node

    @staticmethod
    def _doc_rows(conn: Any) -> list[dict]:
        return [{"id": d["id"], "title": d.get("title") or "", "url": d.get("url")}
                for d in LinearClient._nodes(conn)]

    async def document_get(self, ref: str) -> dict | None:
        """A document by UUID or ``slugId``: ``{id, title, url, content, issue: {id,
        identifier, title} | None, project: {id, name} | None, creator: name | None,
        updated_at}``. ``None`` when Linear says not found (or answers null)."""
        try:
            data = await self.gql(QUERY_DOCUMENT_GET, {"id": ref})
        except LinearError as exc:
            if exc.code == "NOT_FOUND":
                return None
            raise
        d = data.get("document")
        if not isinstance(d, dict) or not d.get("id"):
            return None
        issue, project = d.get("issue"), d.get("project")
        return {
            "id": d["id"], "title": d.get("title") or "", "url": d.get("url"),
            "content": d.get("content") or "",
            "issue": ({"id": issue.get("id"), "identifier": issue.get("identifier"),
                       "title": issue.get("title")} if isinstance(issue, dict) else None),
            "project": ({"id": project.get("id"), "name": project.get("name")}
                        if isinstance(project, dict) else None),
            "creator": (d.get("creator") or {}).get("name"),
            "updated_at": d.get("updatedAt"),
        }

    async def documents_on(self, issue_id: str | None = None,
                           project_id: str | None = None) -> list[dict]:
        """``[{id, title, url, creator_id}]``: the first 250 documents of exactly one of an
        issue or a project (``creator_id`` is ``None`` without a creator, e.g. a deleted
        user). A null issue/project is ``NOT_FOUND``."""
        if (issue_id is None) == (project_id is None):
            raise ValueError("documents_on needs exactly one of issue_id and project_id")
        query, key, ref = ((QUERY_ISSUE_DOCUMENTS, "issue", issue_id) if issue_id is not None
                           else (QUERY_PROJECT_DOCUMENTS, "project", project_id))
        node = (await self.gql(query, {"id": ref})).get(key)
        if not isinstance(node, dict):
            raise LinearError(f"{key} not found", code="NOT_FOUND")
        conn = node.get("documents")
        return [{**row, "creator_id": (d.get("creator") or {}).get("id")}
                for row, d in zip(self._doc_rows(conn), self._nodes(conn))]

    async def document_create(self, title: str, content: str, *, issue_id: str | None,
                              project_id: str | None) -> dict:
        """``documentCreate`` on an issue or a project (at most one); returns ``{id, url}``.
        Linear does not refuse a duplicate title (brainstorm probe)."""
        if issue_id is not None and project_id is not None:
            raise ValueError("document_create takes issue_id or project_id, not both")
        inp: dict[str, Any] = {"title": title, "content": content}
        if issue_id is not None:
            inp["issueId"] = issue_id
        if project_id is not None:
            inp["projectId"] = project_id
        data = await self.gql(MUTATION_DOCUMENT_CREATE, {"input": inp})
        doc = self._payload(data, "documentCreate", "document")
        return {"id": doc["id"], "url": doc.get("url")}

    async def document_update(self, doc_id: str, fields: dict) -> dict:
        """``documentUpdate`` of ``title`` / ``content`` only; returns ``{id, url}``. Any
        other field (``trashed``, ``hiddenAt``, ``issueId``, ``projectId``, …) or none is a
        ``ValueError`` before any request."""
        if not fields or not set(fields) <= DOCUMENT_UPDATE_FIELDS:
            raise ValueError(f"document_update sends only {sorted(DOCUMENT_UPDATE_FIELDS)}")
        data = await self.gql(MUTATION_DOCUMENT_UPDATE, {"id": doc_id, "input": dict(fields)})
        doc = self._payload(data, "documentUpdate", "document")
        return {"id": doc["id"], "url": doc.get("url")}

    async def project_detail(self, project_id: str, updates: int) -> dict:
        """``{id, name, url, state, health, lead, target_date, links, milestones, documents,
        updates}``. ``state`` is ``status.name``, ``lead`` a name; ``updates`` are the newest
        ``updates`` project updates ``{id, created_at, author, health, body}``, newest first
        (sorted here). A null project is ``NOT_FOUND``."""
        data = await self.gql(QUERY_PROJECT_DETAIL, {
            "id": project_id, "updates": max(updates, 1), "withUpdates": updates > 0})
        p = data.get("project")
        if not isinstance(p, dict) or not p.get("id"):
            raise LinearError("project not found", code="NOT_FOUND")
        ups = sorted(self._nodes(p.get("projectUpdates")),
                     key=lambda u: u.get("createdAt") or "", reverse=True)[:max(updates, 0)]
        return {
            "id": p["id"], "name": p.get("name") or "", "url": p.get("url"),
            "state": (p.get("status") or {}).get("name"), "health": p.get("health"),
            "lead": (p.get("lead") or {}).get("name"), "target_date": p.get("targetDate"),
            "links": [{"id": lk.get("id"), "label": lk.get("label"), "url": lk.get("url")}
                      for lk in self._nodes(p.get("externalLinks"))],
            "milestones": [{"id": m.get("id"), "name": m.get("name") or "",
                            "target_date": m.get("targetDate")}
                           for m in self._nodes(p.get("projectMilestones"))],
            "documents": self._doc_rows(p.get("documents")),
            "updates": [{"id": u.get("id"), "created_at": u.get("createdAt"),
                         "author": (u.get("user") or {}).get("name"),
                         "health": u.get("health"), "body": u.get("body") or ""}
                        for u in ups],
        }

    async def project_update_create(self, project_id: str, body: str,
                                    health: str | None) -> dict:
        """``projectUpdateCreate``; ``health`` (``onTrack | atRisk | offTrack``) is left out
        of the input when ``None``. Returns ``{id, url}``. Not idempotent."""
        inp: dict[str, Any] = {"projectId": project_id, "body": body}
        if health is not None:
            inp["health"] = health
        data = await self.gql(MUTATION_PROJECT_UPDATE_CREATE, {"input": inp})
        upd = self._payload(data, "projectUpdateCreate", "projectUpdate")
        return {"id": upd["id"], "url": upd.get("url")}

    @staticmethod
    def _milestone(data: dict, field: str) -> dict:
        m = LinearClient._payload(data, field, "projectMilestone")
        return {"id": m["id"], "name": m.get("name") or "", "target_date": m.get("targetDate")}

    async def milestone_create(self, project_id: str, name: str, description: str | None,
                               target_date: str | None) -> dict:
        """``projectMilestoneCreate``; ``description`` / ``target_date`` are sent only if set.
        Returns ``{id, name, target_date}``. A duplicate name raises Linear's
        ``INPUT_ERROR`` with its ``userPresentableMessage`` as ``detail``."""
        inp: dict[str, Any] = {"projectId": project_id, "name": name}
        if description is not None:
            inp["description"] = description
        if target_date is not None:
            inp["targetDate"] = target_date
        data = await self.gql(MUTATION_MILESTONE_CREATE, {"input": inp})
        return self._milestone(data, "projectMilestoneCreate")

    async def milestone_update(self, milestone_id: str, fields: dict) -> dict:
        """``projectMilestoneUpdate`` of ``name`` / ``description`` / ``targetDate`` only
        (``None`` clears); anything else, or nothing, is a ``ValueError`` before any request.
        Returns ``{id, name, target_date}``."""
        if not fields or not set(fields) <= MILESTONE_UPDATE_FIELDS:
            raise ValueError(f"milestone_update sends only {sorted(MILESTONE_UPDATE_FIELDS)}")
        data = await self.gql(MUTATION_MILESTONE_UPDATE,
                              {"id": milestone_id, "input": dict(fields)})
        return self._milestone(data, "projectMilestoneUpdate")

    async def project_link_create(self, project_id: str, url: str, label: str | None) -> dict:
        """``entityExternalLinkCreate`` on the project (``projectId`` only, never
        ``initiativeId`` / ``releaseId``). ``label`` is required by Linear, so ``None``
        sends the URL as the label. Returns ``{id}``; a duplicate URL is not refused."""
        data = await self.gql(MUTATION_PROJECT_LINK_CREATE, {"input": {
            "projectId": project_id, "url": url, "label": url if label is None else label}})
        return {"id": self._payload(data, "entityExternalLinkCreate", "entityExternalLink")["id"]}
