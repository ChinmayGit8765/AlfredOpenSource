# Connectors: the calendar first

Every capability ALFRED gains on the action side arrives the same way: an
MCP server in `mcp_servers:`, a tier map, and a per-agent allowlist. No
bespoke integrations, ever. This guide walks the canonical first
connector, Google Calendar, end to end; every other connector follows the
same shape (the recipe book is
[config/mcp.example.yaml](../config/mcp.example.yaml)).

Why the calendar first: it is where plans meet reality. With it connected,
agents can check your actual availability before proposing a week, anchor
milestones to events that exist, and put the one next win on your
calendar, all under the same governance as everything else.

## What you need

- Node 18+ (`npx` ships with it): https://nodejs.org
- The MCP extra installed: `uv pip install -e ".[dev,mcp]"`
- A Google account and about ten minutes for the one-time OAuth setup

The server is [@cocal/google-calendar-mcp](https://github.com/nspady/google-calendar-mcp),
the most established Google Calendar MCP server. It runs locally as a
subprocess ALFRED starts and owns; your calendar data flows between
Google and your machine only.

## 1. Create the OAuth client (one time)

1. In the [Google Cloud Console](https://console.cloud.google.com), create
   a project (any name).
2. Enable the **Google Calendar API** for it (APIs and Services, Library).
3. Create credentials: **OAuth client ID**, application type **Desktop
   app**. Add your own Google account as a test user if the consent screen
   asks.
4. Download the JSON and save it somewhere stable, for example
   `C:/Users/you/.config/gcp-oauth.keys.json`.

## 2. Authenticate (one time)

In a terminal (PowerShell shown; opens your browser to approve access):

```powershell
$env:GOOGLE_OAUTH_CREDENTIALS = "C:/Users/you/.config/gcp-oauth.keys.json"
npx -y @cocal/google-calendar-mcp auth
```

Note: while the Google Cloud app is in "testing" publishing status, the
token expires after 7 days and this step must be repeated. Push the app to
"production" in the OAuth consent screen to make the token long-lived; it
stays private to you either way.

## 3. Configure the server

Copy the calendar block from
[config/mcp.example.yaml](../config/mcp.example.yaml) into `mcp_servers:`
in `config/alfred.yaml` and point `GOOGLE_OAUTH_CREDENTIALS` at your JSON
file. The tier map in the recipe is the governance decision: listing and
searching are `read_only` (run automatically), creating and updating are
`reversible_write` (audited, and previewed at first, see below), and
`delete-event` is `destructive` (always asks). Any tool not listed lands
on `destructive` automatically.

## 4. Verify with doctor

```powershell
alfred doctor
```

Doctor genuinely connects to every configured server and reports the live
tool list with its gates:

```
[ok] mcp  server 'calendar' up: 12 tool(s) (7 read_only, 3 reversible_write, 2 destructive)
```

If a tool shows up unclassified, the server's tool names have drifted from
your tier map (they change across server versions); doctor names the
strays so you can fix `tool_tiers`. Unclassified tools still work, they
just ask for confirmation on every call.

## 5. Grant tools to agents

Connecting grants nothing by itself. An agent can only call a tool that is
also on its own manifest allowlist, namespaced `<server>.<tool>`:

```yaml
# agents/training/manifest.yaml
allowed_tools:
  - current_time
  - list_plans
  - calendar.get-freebusy
  - calendar.list-events
  - calendar.create-event
```

Start read-only. Widen to writes when you have seen the reads behave.

## What governance does with it

- `read_only` calls run automatically, always.
- `create-event` and `update-event` are `reversible_write`, but they are
  writes reaching an external system, so while
  `policy.dry_run_cross_system` is on (the default) each one is previewed
  for your confirmation before it runs. Rule on it in chat with
  `confirm <id>` or `deny <id>`. Turn the gate off once the workflow has
  earned trust.
- `delete-event` asks every time, no exceptions.
- When one ask needs several writes (an event AND a note in another
  connector), they surface as one composed intent: a numbered preview you
  `confirm` or `deny` once, executing in order, stopping at the first
  failure. See [GOVERNANCE.md](GOVERNANCE.md).
- Every decision, either way, is in the audit log.

## When things go wrong

- **"did not connect" in doctor**: run the `npx` command from step 2 by
  hand in a terminal; the server's own error output (expired token,
  missing credentials file, no Node) is clearer there.
- **Server dies mid-service**: ALFRED marks it dead, reports tool calls as
  unavailable, and reconnects lazily with a cooldown. No restart needed
  once the underlying problem is fixed.
- **Every call asks for confirmation**: the tool names in `tool_tiers` no
  longer match what the server exposes. Doctor lists the live names.

## Beyond the calendar

The same five steps connect a filesystem, an Obsidian vault, GitHub, home
automation, or anything else the MCP ecosystem publishes
(https://github.com/modelcontextprotocol/servers). Copy a recipe, map the
tiers, allowlist per agent, verify with doctor. Every server is a new
ALFRED capability with zero new ALFRED code.
