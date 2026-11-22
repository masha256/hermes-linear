# Hermes Linear Channel

> **Status: pre-release, in development.** This README describes the plugin as designed in
> [`docs/superpowers/specs/2026-11-21-hermes-linear-channel-design.md`](docs/superpowers/specs/2026-11-21-hermes-linear-channel-design.md).
> Commands and settings below are the target interface; until the first release, treat them as a spec.

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) channel plugin that turns Linear into a place you work *with* your Hermes agent:

- **Delegate a ticket to your agent → one Hermes session for the whole ticket.** Every agent thread on the ticket, plain comments from your team, and edits to the ticket and its project all feed the same conversation, so the agent remembers everything about the ticket.
- **@mention your agent on any other ticket → a quick, thread-scoped Q&A.** Follow-ups in that thread keep context; nothing is shared ticket-wide.
- **Native Linear agent UI:** progress (thoughts and tool calls), approvals and questions as pick-lists, the Stop button, and a live plan checklist.

It runs inside *your* Hermes and connects to *your* Linear workspace through a Linear OAuth app you create. Nothing is hosted by anyone else.

---

## Contents

- [How it behaves](#how-it-behaves)
- [Requirements](#requirements)
- [Install](#install)
- [Create the Linear OAuth app](#create-the-linear-oauth-app)
- [Expose the webhook with Tailscale Funnel](#expose-the-webhook-with-tailscale-funnel)
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
| Start a new thread on a delegated ticket (with or without @mention) | The agent picks it up in the **same** ticket session and answers in that thread. |
| Edit the ticket (status, labels, description, links) or its project, or post a project update | Recorded as context. The agent sees a "Since your last turn" summary at the start of its next turn. Edits never wake the agent on their own. |
| @mention the agent on a ticket that is *not* delegated | A one-off session for that thread only. |
| Press **Stop** | The running turn is interrupted; the agent reports what it finished and what it didn't. |
| Answer an approval / question pick-list | The agent continues. Free text never counts as approval of a risky action. |
| Remove the delegation | Any running turn stops. The session's memory is kept; re-delegating resumes it — unless the ticket is Done or Canceled, which starts a fresh session. |

Everything the agent does appears in Linear's agent session panel: every tool call as an *action* with its result preview, commentary as *thoughts*, the final answer as a comment in the thread. Two prompts arriving at once (e.g. from two threads) are queued, never interleaved.

## Requirements

- Hermes Agent with the gateway running (`hermes gateway run` or the systemd service).
- A Linear workspace where you can create an OAuth application (workspace admin).
- A way for Linear to reach your machine over HTTPS. This guide uses [Tailscale Funnel](https://tailscale.com/kb/1223/funnel); any tunnel or reverse proxy works.

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

## Expose the webhook with Tailscale Funnel

The plugin runs one local listener (default `127.0.0.1:8650`) for all profiles. Publish it under the `/linear/` path:

```bash
tailscale funnel --bg --https=8443 --set-path /linear http://127.0.0.1:8650/linear
tailscale funnel status
```

- Funnel only serves ports **443, 8443, 10000**. Pick one not already used by `tailscale serve`.
- If your tailnet hasn't enabled Funnel yet, the command prints an enable link — open it once.
- Your webhook URL is then `https://<machine>.<tailnet>.ts.net:8443/linear/<profile>/webhook`. `hermes linear setup` prints the exact URL.
- To stop exposing it: `tailscale funnel --https=8443 off` (this turns off everything on that port).

## Configure the plugin

Easiest: run the wizard and follow the prompts.

```bash
hermes linear setup            # or: hermes gateway setup → Linear
# for another profile:
hermes -p <profile> linear setup
```

The wizard stores secrets in the profile's `.env`, other settings in the profile's `config.yaml`, prints the webhook URL, and runs a live check. Restart the gateway afterwards.

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
| `session_url` | string | `""` | Optional link shown as **Open** on the Linear agent session, e.g. `https://hermes.example.com/sessions/{session_id}`. Empty = no link. |
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

- **Agent shows "unresponsive" in Linear.** Linear needs a first response within 10 s. Check the gateway is running, `hermes linear status` shows a recent event, and the Funnel is up (`tailscale funnel status`).
- **No events arriving.** Confirm the webhook URL in the Linear app matches `hermes linear status` exactly (including the profile name and port), and that *Agent session events* is ticked.
- **401s in the gateway log.** The webhook signing secret doesn't match `LINEAR_WEBHOOK_SECRET`, the machine clock is off (replay window is 60 s), or a webhook URL for one profile was pasted into another profile's app.
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

## Limitations

- Webhooks only; there is no polling mode, so Linear must be able to reach your listener.
- Documents, initiatives, customers and project-level agent sessions are not handled.
- File uploads in Linear appear to the agent as links inside the ticket description or comment, not as attachments.

## Development

Design spec: [`docs/superpowers/specs/2026-11-21-hermes-linear-channel-design.md`](docs/superpowers/specs/2026-11-21-hermes-linear-channel-design.md). Work is tracked in the Linear project **hermes-linear** (formerly Valkyrie); dogfooding happens in **hermes linear test project**. Branching, the live checkout and the test-before-PR loop: [`docs/development.md`](docs/development.md).
