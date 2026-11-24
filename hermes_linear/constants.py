"""Internal constants (not user settings)."""

REPLAY_WINDOW_S = 60
MAX_BODY_BYTES = 1_048_576
DEDUP_TTL_S = 86_400
DELEGATION_WAIT_S = 5
FAST_TOOL_SKIP_S = 2
DIGEST_CAP_CHARS = 4_000
MAX_PENDING_ITEMS = 200
HEARTBEAT_S = 600
TYPING_MIN_INTERVAL_S = 60
DESCRIPTION_SNIPPET = 800
SNAPSHOT_DESCRIPTION = 2_000
RESULT_PREVIEW = 500
USER_CACHE_TTL_S = 3_600
TOKEN_RENEW_MARGIN_S = 86_400
TEST_WEBHOOK_PREFIX = "hermes-linear-test-"  # webhookId prefix of `hermes linear test` probes
WRITE_RETRIES = 3
ACK_TIMEOUT_S = 8  # Linear calls on the event worker must finish inside Linear's 10 s window
TURN_SAFETY_TIMEOUT_S = 3 * 3600  # a handed-off turn that never completes frees its FIFO
STOP_SETTLE_S = 30  # a stopped turn the gateway never completes is answered and freed (Q6)
TOOL_TIMEOUT_S = 30.0  # agent tool bridge: bounded wait for a Linear call on the adapter loop
#: Loop-side budget for one plan sync. A ~30-task plan costs up to ~30 issue reads, ~30
#: writes and a few comment calls, one GraphQL round trip each (0.3–1 s live): more than
#: the generic TOOL_TIMEOUT_S (30 s). Every step is idempotent, so a TIMEOUT is retried
#: safely (children are adopted by their ``T<n> ·`` title on the rerun). Also the budget
#: of a ``linear_attach`` upload (a 50 MB PUT; RET-120 spec §2.4).
PLAN_SYNC_TIMEOUT_S = 120.0
#: Timeout of the ``linear_attach`` storage PUT: below PLAN_SYNC_TIMEOUT_S so a slow PUT
#: fails as UPLOAD_FAILED/NETWORK inside the tool budget, not as a tool TIMEOUT.
UPLOAD_PUT_TIMEOUT_S = 100.0
