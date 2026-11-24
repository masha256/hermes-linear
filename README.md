# Hermes Linear Channel

> **Status: pre-release, in development.** This README describes the plugin as designed in
> [`docs/superpowers/specs/2026-11-21-hermes-linear-channel-design.md`](docs/superpowers/specs/2026-11-21-hermes-linear-channel-design.md).
> Commands and settings below are the target interface; until the first release, treat them as a spec.

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) channel plugin that turns Linear into a place you work *with* your Hermes agent:

- **Delegate a ticket to your agent → one Hermes session for the whole ticket.** The agent works in the thread Linear opens on delegation (and in any thread where you @mention it). Plain comments from your team and edits to the ticket and its project reach the same conversation as context, so the agent remembers everything about the ticket.
- **@mention your agent on any other ticket → a quick, thread-scoped Q&A.** Follow-ups in that thread keep context; nothing is shared ticket-wide.
- **Native Linear agent UI:** progress (thoughts and tool calls), approvals and questions as pick-lists, the Stop button, and a live plan checklist.

It runs inside *your* Hermes and connects to *your* Linear workspace through a Linear OAuth app you create. Nothing is hosted by anyone else.

---

## Contents

- [How it behaves](#how-it-behaves)
- [Agent tools](#agent-tools)
- [Requirements](#requirements)
- [Install](#install)
- [Create the Linear OAuth app](#create-the-linear-oauth-app)
- [Expose the webhook](#expose-the-webhook)
- [Configure the plugin](#configure-the-plugin)
- [Configuration reference](#configuration-reference)
- [Multiple Hermes profiles](#multiple-hermes-profiles)
- [Access control](#access-control)
- [CLI](#cli)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)
- [Limitations](#limitations)

---

## How it behaves

| You do in Linear | What happens |
|---|---|
| Delegate a ticket to the agent | A Hermes session opens for the ticket. The agent acknowledges within seconds, moves the ticket to your team's first *started* status (configurable), and starts working. |
| Reply in the agent's thread | The reply is a new turn in the ticket's session. |
| Comment on a delegated ticket without @mentioning the agent (a new thread, or a reply in a thread that isn't the agent's) | Recorded as context, like an edit: the agent sees it in the "Since your last turn" summary of its next turn. Plain comments never wake the agent; threads outside the agent's own are for people. |
| @mention the agent in another thread on a delegated ticket | The agent joins that thread in the **same** ticket session and answers there. |
| Edit the ticket (status, labels, description, links) or its project, or post a project update | Recorded as context. The agent sees a "Since your last turn" summary at the start of its next turn. Edits never wake the agent on their own. |
| @mention the agent on a ticket that is *not* delegated | A one-off session for that thread only. |
| Press **Stop** | The running turn is interrupted; the agent reports what it finished and what it didn't. |
| Answer an approval / question pick-list | The agent continues. Free text never counts as approval of a risky action. |
| The agent wants to write to a ticket outside its own (not the session ticket, its sub-tickets or tickets it created from it) | It asks first: an **Allow once / Deny** pick-list in the thread names the tool, the target ticket and the arguments. *Allow once* runs exactly that call; any other call asks again. Unanswered within Hermes's `approvals.timeout`, the question stays pending: answer it later and the agent re-runs the call without a second prompt. Set with `out_of_scope_writes`. |
| The agent attaches a file or link (e.g. its PR) | It shows in the ticket's attachments, added by the app. The agent also links its PR on the agent session (`linear_session_link`). Only files Hermes would deliver as media are uploaded (see [Attachments](#attachments)); another ticket asks first, like any out-of-scope write. |
| Remove the delegation | Any running turn stops. The session's memory is kept; re-delegating resumes it — unless the ticket is Done or Canceled, which starts a fresh session. |

Everything the agent does appears in Linear's agent session panel: every tool call as an *action* with its result preview, commentary as *thoughts*, the final answer as a comment in the thread. Two prompts arriving at once (e.g. from two threads) are queued, never interleaved.

## Agent tools

The plugin gives the agent these tools. They act as the Linear app (every write is attributed to it), never raise, and answer `{"ok": false, "error": "<CODE>"}` on failure.

| Tool | What it does |
|---|---|
| `linear_issue_get` | Read any issue: state, labels, project and its links, parent, children, assignee, delegate, priority, estimate, due date, cycle, milestone, relations (both directions) and attachments. `comments: N` (0–50, default 0) adds the newest N comment threads, newest first, each with its replies oldest first; past 50,000 characters the oldest threads are dropped and `comments_truncated` is `true`. |
| `linear_search` | Find issues: full-text `text` (optionally `include_comments`), or the most recently updated issues. Typed filters, ANDed: `team` (key), `project` (UUID or name), `state` (workflow type or state name), `assignee` / `delegate` (`me`, an email or a name), `label`, `parent`. Pages of `limit` rows (1–50, default 25) with `next_cursor`; anything more complex goes through `linear_query`. |
| `linear_issue_create` | Create an issue in the session ticket's team and project, optionally under a `parent`. |
| `linear_issue_update` | Change title, description, state, labels or parent. |
| `linear_comment` | Comment on an issue, or edit the app's own marked comment. |
| `linear_sync_spec`, `linear_sync_plan` | Render a committed spec / plan into the ticket description, the plan comment and task sub-tickets. |
| `linear_task_state` | Move a task sub-ticket along (`in_progress`, `in_review`, `done`, `blocked`, `unblocked`). |
| `linear_query` | Run a read-only GraphQL query as the app (mutations and subscriptions are refused; a result over 50,000 characters is refused with `RESPONSE_TOO_LARGE`). Works in any session. |
| `linear_attach` | Attach to an issue: `path` + `id` uploads a local file (see [Attachments](#attachments)); `url` + `id` links a URL (a GitHub PR URL as a PR attachment; a URL the issue already has answers `ALREADY_LINKED`); `path` + `embed: true` (no `id`) only uploads and returns `markdown` to put in a comment. |
| `linear_session_link` | Show a link (usually the PR) on the current turn's Linear agent session. Label defaults to `Pull request` for a GitHub PR URL, else the URL's host; re-adding the same URL is a no-op. |

**Write scope.** In a Linear session, writes go without asking to the session ticket, its descendants, and tickets the app created from it. A `linear_comment`, `linear_issue_update`, `linear_issue_create` or `linear_attach` aimed elsewhere first answers `APPROVAL_REQUIRED`; the agent's identical re-call asks a human in the thread (see [How it behaves](#how-it-behaves) and `out_of_scope_writes`). `linear_sync_spec`, `linear_sync_plan` and `linear_task_state` refuse any other ticket (`OUT_OF_SCOPE`). Outside a Linear session (e.g. Slack) the write tools refuse (`NOT_LINEAR_SESSION`): an approval given anywhere but the ticket thread never authorizes a Linear write. `linear_attach` with `embed` and `linear_session_link` target no issue, so they have no scope check, but they too need a Linear session (`NOT_LINEAR_SESSION`). Reads (`linear_issue_get`, `linear_search`, `linear_query`) work from any session.

### Attachments

`linear_attach` uploads a file only if Hermes would deliver it as media to any other channel: the path must be absolute, and Hermes's media-delivery policy (`validate_media_delivery_path`) must accept it. Symlinks are judged and read by their target. By default that is any existing regular file outside a denylist: `.env` files anywhere, `~/.ssh`, `~/.aws`, `~/.gnupg` and the other credential dirs, system dirs such as `/etc`, and Hermes's own state and config (`config.yaml`, `auth.json`, `state.db`, `sessions/`, …). Refused paths answer `PATH_DENIED`; there is no approval for them. In strict mode (`gateway.strict: true` or `HERMES_MEDIA_DELIVERY_STRICT=1`) only the Hermes cache dirs, the dirs in `HERMES_MEDIA_ALLOW_DIRS` and freshly produced files pass. Strict mode also closes the default mode's gap: a credential file outside the denylisted dirs (e.g. `~/.netrc`) passes in default mode.

- Files over 50 MB are refused (`FILE_TOO_LARGE`).
- Attaching the same file again adds a second attachment and a second upload (Linear's API cannot delete uploads). Re-linking a URL doesn't: it answers `ALREADY_LINKED`, and the issue keeps one attachment.
- On another ticket, a file or URL attach asks for approval first, like any out-of-scope write. If the upload worked but attaching it failed, the error carries `asset_url` so the agent can embed the file instead.
- A local file named in a final reply (`MEDIA:`) is still shown by its basename and never uploaded; `linear_attach` is the only upload path.

## Requirements

- Hermes Agent with the gateway running (`hermes gateway run` or the systemd service).
- A Linear workspace where you can create an OAuth application (workspace admin).
- A way for Linear to reach your machine over HTTPS: Tailscale Funnel, Cloudflare Tunnel, ngrok, or a reverse proxy on a public host. See [Expose the webhook](#expose-the-webhook).

## Install

```bash
hermes plugins install masha256/hermes-linear   # pin with --ref <commit-sha> for reproducible installs
hermes plugins enable linear
```

Third-party plugins are disabled until you enable them.

## Create the Linear OAuth app

Create **one app per Hermes profile** you want in Linear. The app's name and icon are how the agent appears in Linear (mention menu, assignee/delegate menu), so pick something short and recognisable.

1. In Linear go to **Settings → API → Applications → New application** (<https://linear.app/settings/api/applications/new>).
2. Fill in **Name**, **Icon**, and **Developer URL**. If Linear asks for a **Callback URL**, any HTTPS URL on your host works (e.g. `https://<your-host>/linear/oauth/unused`) — the plugin doesn't use the browser OAuth flow.
3. **Enable client credentials** for the app. The plugin uses client-credentials tokens (30-day, app identity, no browser sign-in).
4. **Enable webhooks** and set the URL to your profile's webhook URL:
   ```
   https://<your-host>/linear/<hermes-profile>/webhook
   ```
   For a single-profile install, `<hermes-profile>` is `default`.
5. Tick these webhook categories:
   - **Agent session events** — required; this is what turns the app into an agent
   - **Issues**
   - **Comments**
   - **Issue attachments**
   - **Projects**
   - **Project updates**

   *Issue labels* and *Project labels* are not needed (they describe label definitions; labels applied to a ticket arrive with Issues).
6. Save, then copy the **Client ID**, **Client secret**, and **Webhook signing secret**.
7. Team access: client-credentials tokens can reach all **public** teams. To use the agent in a private team, add that team on the app's page under **Settings → Applications**.

The plugin requests the scopes `read, write, app:mentionable, app:assignable`.

## Expose the webhook

The plugin runs one local listener (default `127.0.0.1:8650`) for all profiles, with a single route: `POST /linear/<profile>/webhook`. Linear must reach it over public HTTPS. Any tunnel or proxy works as long as it forwards to the listener **with the path unchanged**. Forwarding only `/linear/` is enough; nothing else is served.

Your webhook URL is always `https://<public_host>/linear/<profile>/webhook`. `hermes linear setup` asks for `public_host` (it suggests your Tailscale name when Tailscale is installed; overwrite it for any other option) and prints the exact URL.

No extra auth is needed at the tunnel: every webhook is HMAC-signed and rejected if older than 60 s.

| Option | Setup | Stable URL | Needs your own domain | Notes |
|---|---|---|---|---|
| [Tailscale Funnel](#tailscale-funnel) | One command | Yes | No | Ports 443, 8443, 10000 only; URL carries the port. |
| [Cloudflare Tunnel](#cloudflare-tunnel) | Moderate | Yes (named tunnel) | Yes, on Cloudflare DNS | No open ports; quick tunnels are testing-only. |
| [ngrok](#ngrok) | One command | Yes (dev domain) | No | Free plan: one static dev domain. |
| [Reverse proxy on a public host](#reverse-proxy-on-a-public-host) | Most work | Yes | Yes | VPS or public IP; Caddy handles TLS. |

### Tailscale Funnel

Publish the listener under the `/linear/` path:

```bash
tailscale funnel --bg --https=8443 --set-path /linear http://127.0.0.1:8650/linear
tailscale funnel status
```

- Funnel only serves ports **443, 8443, 10000**. Pick one not already used by `tailscale serve`.
- If your tailnet hasn't enabled Funnel yet, the command prints an enable link — open it once.
- `public_host` is `<machine>.<tailnet>.ts.net:8443`, so the webhook URL is `https://<machine>.<tailnet>.ts.net:8443/linear/<profile>/webhook`.
- To stop exposing it: `tailscale funnel --https=8443 off` (this turns off everything on that port).

### Cloudflare Tunnel

Use a **named tunnel** on a domain whose DNS is on Cloudflare. `cloudflared` connects outbound, so no inbound ports are opened. See Cloudflare's [locally-managed tunnel guide](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/local-management/create-local-tunnel/) for installing `cloudflared`.

```bash
cloudflared tunnel login                       # once; opens a browser to pick the zone
cloudflared tunnel create hermes               # prints the tunnel UUID and credentials file
cloudflared tunnel route dns hermes hermes.example.com
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: <tunnel-uuid>
credentials-file: /home/<you>/.cloudflared/<tunnel-uuid>.json
ingress:
  - hostname: hermes.example.com
    path: ^/linear/
    service: http://127.0.0.1:8650
  - service: http_status:404
```

```bash
cloudflared tunnel run hermes                  # foreground, to test
sudo cloudflared service install               # then run it as a service
```

- `public_host` is `hermes.example.com` (no port); the webhook URL is `https://hermes.example.com/linear/<profile>/webhook`.
- The service reads its config from `/etc/cloudflared/` when installed as root; copy `config.yml` and the credentials file there if it can't find them. See [run as a service](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/local-management/as-a-service/).
- **Quick tunnels** (`cloudflared tunnel --url http://127.0.0.1:8650`) give a random `*.trycloudflare.com` URL that changes on every restart, so you'd re-edit the Linear app each time. Testing only.

### ngrok

Every ngrok account, free included, gets one static **dev domain** (e.g. `your-name.ngrok-free.dev`; find it under *Domains* in the ngrok dashboard). Paid plans can use a reserved or custom domain via `--url`.

```bash
ngrok config add-authtoken <token>             # once
ngrok http 8650 --url your-name.ngrok-free.dev # foreground, to test
```

To keep it running, define the endpoint in the agent config (`ngrok config edit`):

```yaml
version: 3
agent:
  authtoken: <token>
endpoints:
  - name: hermes-linear
    url: https://your-name.ngrok-free.dev
    upstream:
      url: 8650
```

```bash
sudo ngrok service install --config /home/<you>/.config/ngrok/ngrok.yml
sudo ngrok service start
```

`ngrok config check` prints the config file's path if yours lives elsewhere (macOS: `~/Library/Application Support/ngrok/ngrok.yml`).

- `public_host` is `your-name.ngrok-free.dev` (no port).
- The free plan's browser warning page is shown to browsers only; Linear's webhook requests are not affected.
- This exposes the whole listener port, not just `/linear/`. That is fine (the listener serves nothing else); restrict it with an ngrok traffic policy if you want.

### Reverse proxy on a public host

If you have a VPS or a machine with a public IP and a DNS name, put [Caddy](https://caddyserver.com/) in front of the listener; it obtains and renews the TLS certificate itself. `Caddyfile`:

```caddyfile
hermes.example.com {
	handle /linear/* {
		reverse_proxy 127.0.0.1:8650
	}
	respond 404
}
```

- Hermes on the same host: done. `public_host` is `hermes.example.com`.
- Hermes behind NAT (home box) and Caddy on a VPS: carry the listener port over SSH from the Hermes machine, and keep it up with `autossh` or a systemd unit:

  ```bash
  ssh -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
      -R 127.0.0.1:8650:127.0.0.1:8650 you@vps.example.com
  ```

- nginx or any other proxy works the same way: TLS termination, forward `/linear/` to the listener, path unchanged.

Not recommended: ad-hoc tunnel services with random URLs or interstitial pages (localtunnel and similar). A changing URL means re-editing the Linear app, and an interstitial breaks delivery.

## Configure the plugin

Easiest: run the wizard and follow the prompts.

```bash
hermes linear setup            # or: hermes gateway setup → Linear
# for another profile:
hermes -p <profile> linear setup
```

The wizard stores secrets in the profile's `.env`, other settings in the profile's `config.yaml`, prints the webhook URL, and runs a live check. Restart the gateway afterwards. Its step 3 prints a Tailscale Funnel command; skip it if you expose the webhook another way.

Manual setup is equivalent:

```bash
hermes config set plugins.entries.linear.settings.client_id <client-id>
# secrets go in the profile's .env (never in config.yaml):
#   LINEAR_CLIENT_SECRET=...
#   LINEAR_WEBHOOK_SECRET=...
hermes config set plugins.entries.linear.settings.allowed_users "you@example.com,val@example.com"
```

In the Hermes Desktop app, all settings (including masked secret fields) are under **Settings → Plugins → Linear**.

## Configuration reference

Settings live at `plugins.entries.linear.settings.<key>` in each profile's `config.yaml`. Secrets live only in that profile's `.env`.

| Setting | Type | Default | Description |
|---|---|---|---|
| `client_id` | string | — (required) | Client ID of this profile's Linear OAuth app. |
| `client_secret` | secret (`LINEAR_CLIENT_SECRET`) | — (required) | Client secret of the app. Used to obtain client-credentials tokens. |
| `webhook_secret` | secret (`LINEAR_WEBHOOK_SECRET`) | — (required) | Webhook signing secret from the app. Every webhook's `Linear-Signature` is verified against it. |
| `allowed_users` | list | `[]` | Who may drive the agent: Linear user UUIDs, emails, or `*` for everyone. Accepts a list or a comma-separated string. `LINEAR_ALLOWED_USERS` (comma-separated) in `.env` is also honoured; the two are combined. Empty and `allow_all_users` off = nobody. |
| `allow_all_users` | bool | `false` | Allow every workspace member with access to the app's teams. `LINEAR_ALLOW_ALL_USERS=true` is also honoured. |
| `auto_start` | bool | `true` | When a human delegates a ticket that is in Backlog / Todo / Triage, move it to the team's first *started* status when work begins. Tickets delegated by automations are left alone. |
| `public_host` | string | `""` | Public HTTPS host (optionally `:port`) that Linear reaches the listener on. Used to build and print the webhook URL (`https://<public_host>/linear/<profile>/webhook`); set by `hermes linear setup`. Empty = Tailscale name if detected. |
| `session_url` | string | `""` | Optional link shown as **Open** on the Linear agent session, e.g. `https://hermes.example.com/sessions/{session_id}`. Empty = no link. |
| `out_of_scope_writes` | string | `approve` | What a Linear session does when an agent tool would write to an issue outside its write scope: `approve` asks a human in Linear first, `deny` refuses (`OUT_OF_SCOPE`), `allow` writes without asking (trusted single-user setups only). Case-insensitive. |
| `listener_host` | string | `127.0.0.1` | Bind address of the shared webhook listener. **Read from the default profile only.** |
| `listener_port` | int | `8650` | Port of the shared webhook listener. **Read from the default profile only.** |

Display verbosity follows Hermes's standard per-platform display settings (`display.platforms.linear`). The default shows every tool call.

Fixed behaviour (not configurable): 60 s webhook replay window, 1 MB body limit, 24 h webhook de-duplication, ~4,000-character context summary per turn, heartbeat every 10 minutes on long turns.

## Multiple Hermes profiles

Each profile gets its own Linear app, credentials, allowlist, sessions and state; all share one listener.

1. Create one Linear OAuth app per profile (above).
2. Set each app's webhook URL to `https://<host>/linear/<that-profile>/webhook`.
3. Run `hermes -p <profile> linear setup` for each profile.

Two apps can live in the same Linear workspace. Each only acts on tickets delegated to it or threads that mention it. Renaming a Hermes profile changes its webhook URL — update the Linear app afterwards (`hermes -p <profile> linear status` prints the current URL).

## Access control

- Default is **deny**: nobody can use the agent until you set `allowed_users` or `allow_all_users`.
- Entries can be Linear user UUIDs, emails, or `*`. Display names are never matched.
- A user who isn't allowed gets a short refusal in Linear and no agent turn runs. Their comments and edits are also **not** passed to the agent as context.
- Bots and integrations (e.g. GitHub marking a ticket Done) are passed as context but never wake the agent.
- Who may answer approval prompts follows Hermes's `approvals.approvers` setting (default: any allowed user).

## CLI

| Command | Purpose |
|---|---|
| `hermes linear setup` | Interactive setup for the current profile. |
| `hermes linear status` | Token validity and expiry, app user, webhook URL, last event received, access mode, errors. |
| `hermes linear test` | Sends a locally signed test event through the listener to check signature verification and routing (no Linear involved). |

Use `hermes -p <profile> linear …` for other profiles.

## Troubleshooting

- **Agent shows "unresponsive" in Linear.** Linear needs a first response within 10 s. Check the gateway is running, `hermes linear status` shows a recent event, and the tunnel is up (`tailscale funnel status`, `cloudflared tunnel info <name>`, or the ngrok dashboard).
- **No events arriving.** Confirm the webhook URL in the Linear app matches `hermes linear status` exactly (including the profile name and port), and that *Agent session events* is ticked.
- **401s in the gateway log.** The webhook signing secret doesn't match `LINEAR_WEBHOOK_SECRET`, the machine clock is off (replay window is 60 s), or a webhook URL for one profile was pasted into another profile's app.
- **Tunnel up but nothing reaches the listener.** Test from outside: `curl -i -X POST https://<public_host>/linear/<profile>/webhook` should return a 401 from the plugin (unsigned request). A 404 from the tunnel or proxy means the path was rewritten or not routed; a 502 means it can't reach `127.0.0.1:8650`.
- **404s.** The profile in the URL doesn't exist or the Linear channel isn't enabled for it.
- **"Not authorized" replies.** Add the user to `allowed_users` (UUID or email).
- **Agent can't see a private team.** Add the team on the app's page in Linear settings.
- **Tool names like "📐 linear_issue_get..." posted as replies.** Tool calls already show as Linear *action* activities. The plugin defaults `tool_progress` to off for Linear, but a global `display.tool_progress` in your config overrides that default. Add this to the profile's `config.yaml`:
  ```yaml
  display:
    platforms:
      linear:
        tool_progress: "off"
  ```
- **Token errors.** `hermes linear status` shows token state. Rotating the client secret in Linear invalidates tokens; update `LINEAR_CLIENT_SECRET` and restart the gateway.

## Security notes

- Every webhook is HMAC-verified, rejected if older than 60 s, and de-duplicated.
- Secrets are stored only in the profile's `.env`; tokens are held in memory.
- Ticket text is untrusted input. Keep the allowlist tight and keep Hermes approvals on for risky tools; consider a narrower toolset for the Linear platform (`hermes tools … --platform linear`).
- An out-of-scope write (`out_of_scope_writes: approve`) goes to a human through Hermes's own approval gate; one approval covers one call with identical arguments. Hermes's bypasses apply to it as to every approval: `/yolo` (or `--yolo`) and `approvals.mode: off` approve it without asking. Hermes's presence rules decide who is asked: where no human is present (cron, a non-gateway run) its gate blocks the write (cron follows `approvals.cron_mode`); a subagent that inherits the gateway session asks in Linear like its parent. `out_of_scope_writes: allow` skips the question entirely; use it only in trusted single-user setups.

## Limitations

- Webhooks only; there is no polling mode, so Linear must be able to reach your listener.
- Documents, initiatives, customers and project-level agent sessions are not handled.
- File uploads in Linear appear to the agent as links inside the ticket description or comment, not as attachments.

## Development

Design spec: [`docs/superpowers/specs/2026-11-21-hermes-linear-channel-design.md`](docs/superpowers/specs/2026-11-21-hermes-linear-channel-design.md). Work is tracked in the Linear project **hermes-linear** (formerly Valkyrie); dogfooding happens in **hermes linear test project**. Branching, the live checkout and the test-before-PR loop: [`docs/development.md`](docs/development.md).
