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
