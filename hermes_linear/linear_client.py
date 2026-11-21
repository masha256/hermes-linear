"""Async Linear GraphQL client using OAuth client-credentials app tokens.

Self-contained (aiohttp + stdlib only; no Hermes imports). Never logs or
embeds tokens/secrets in exceptions.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any, Awaitable, Callable

import aiohttp

from .constants import TOKEN_RENEW_MARGIN_S, WRITE_RETRIES

log = logging.getLogger(__name__)

SCOPES = "read,write,app:mentionable,app:assignable"
_HTTP_TIMEOUT_S = 30
_MAX_RETRY_AFTER_S = 60.0
_DEFAULT_RETRY_AFTER_S = 1.0
_AUTH_CODES = {"AUTHENTICATION_ERROR", "UNAUTHENTICATED"}
_RATELIMIT_CODE = "RATELIMITED"

# -- GraphQL documents (verified against the Linear SDL) ---------------

QUERY_VIEWER = """query Viewer { viewer { id name } }"""

QUERY_USER = """query User($id: String!) { user(id: $id) { email name } }"""

MUTATION_ACTIVITY = """mutation AgentActivityCreate($input: AgentActivityCreateInput!) {
  agentActivityCreate(input: $input) { success agentActivity { id } }
}"""

MUTATION_SESSION_ON_COMMENT = """mutation AgentSessionCreateOnComment($input: AgentSessionCreateOnComment!) {
  agentSessionCreateOnComment(input: $input) { success agentSession { id } }
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
  issueUpdate(id: $id, input: $input) { success }
}"""


class LinearError(Exception):
    """A Linear API failure. ``code`` is the GraphQL/OAuth error code if any."""

    def __init__(self, message: str = "", *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"LinearError({str(self)!r}, code={self.code!r}, status={self.status!r})"


def _first_error(body: Any) -> tuple[str | None, str]:
    errors = body.get("errors") if isinstance(body, dict) else None
    if not errors:
        return None, ""
    err = errors[0] if isinstance(errors[0], dict) else {}
    code = (err.get("extensions") or {}).get("code")
    return code, str(err.get("message") or "GraphQL error")


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
            code, message = _first_error(body)

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
                raise LinearError(message, code=code, status=status)
            if status >= 400 or not isinstance(body, dict) or not isinstance(body.get("data"), dict):
                raise LinearError(f"unexpected GraphQL response (HTTP {status})", status=status)
            return body["data"]

    # -- API ------------------------------------------------------------

    async def viewer_id(self) -> str:
        if self._viewer_id is None:
            data = await self.gql(QUERY_VIEWER)
            self._viewer_id = str(data["viewer"]["id"])
        return self._viewer_id

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

    async def create_session_on_comment(self, comment_id: str) -> str:
        data = await self.gql(MUTATION_SESSION_ON_COMMENT, {"input": {"commentId": comment_id}})
        return self._require_success(data, "agentSessionCreateOnComment")["agentSession"]["id"]

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
    ) -> None:
        inp: dict[str, Any] = {}
        if plan is not None:
            inp["plan"] = plan
        if external_urls is not None:
            inp["externalUrls"] = external_urls
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
