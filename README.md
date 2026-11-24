# Vantage Codex plugin

This repository is the local Codex plugin marketplace for Vantage.

## Test

Run the dependency-free Python test suites:

```sh
./bin/test
```

## Install from this checkout

From the repository root:

```sh
./bin/install-local
```

The installer replaces any existing local `vantage-codex-plugin` marketplace registration,
installs `vantage@vantage-codex-plugin` from this checkout, and prints the active source.
Start a new Codex session after installation so Codex reloads the plugin.

After editing the plugin, run `./bin/install-local` again to refresh the installed copy.

## Connect Codex

In Codex, ask **“Set up Vantage telemetry.”** Complete the browser sign-in,
trust the Vantage hooks in Settings > Hooks, and restart Codex. Setup uses
the normal Codex home (`~/.codex`, or your existing `CODEX_HOME`) and merges
the handlers into `hooks.json`. The stable launcher resolves the installed
plugin hook. To disconnect, ask Codex to remove Vantage telemetry.

If you configured telemetry with an earlier version, rerun setup and review
the updated hooks to apply the shell and edit tool filter.

The current pre-release ingest destination is **ingest-staging.vantage.sh**.
Setup writes the native OTLP logs exporter and the attribution sender's
connection from the same endpoint and credential. The sender uses verified
TLS and OTLP/gRPC. The bundled tools start with `python3` on `PATH` and require
Python 3.9+ with SQLite support and curl with HTTP/2 support.

On Windows, use the [Python Install Manager](https://docs.python.org/3/using/windows.html)
and confirm that `python3 --version` reports Python 3.9 or newer. The MCP
server and installed Windows hooks use the same `python3` command.
If `python3` opens the Microsoft Store, check that the Python Install Manager
is installed and its app execution aliases are enabled. Check that
`curl.exe --version` lists `HTTP2` under Features. If the bundled Windows curl
lacks HTTP/2, install an [HTTP/2-enabled curl build](https://curl.se/windows/),
set `VANTAGE_CODEX_CURL` to its full executable path, and restart Codex.
Keep `CODEX_HOME` in a private directory in your Windows user profile so
credentials inherit its access permissions.

## What gets recorded

Repository, worktree, branch, and commit attribution use Git. If Git is
unavailable, setup shows a warning and continues.
Usage reporting and classifier tags can still work.

Each user prompt opens a turn snapshot. The hook captures Git and developer
identity observations (including the effective Git `user.name` and `user.email`), model request fingerprints, and classifier tags. Setup
installs the account classifier and refreshes its cached Markdown on session
start. Within a session, a new turn inherits the previous prompt's classification
when the worktree, branch, and classifier contents match. Ordinary commits on
a branch preserve the classification. Detached checkouts use the commit, and
outside Git the hook uses the working directory. A task change in the same
checkout can override inherited tags with the plugin’s `set_turn_attribution` MCP tool.
An unchanged follow-up requires no classifier read or tool call from the agent.
The tool writes through the same launcher that handles hook events. Codex can
ask for approval for this write. For unattended runs, authorize `vantage_local.set_turn_attribution`
in the host's tool approval settings before starting the session. When the host
blocks the tool, new classifier tags cannot be saved.

The hook checks for Git changes after shell and edit tools finish. It reuses
local observations while the worktree's HEAD, current ref, and Git configuration
are unchanged.

Hooks commit evidence to a private SQLite outbox under
`${CODEX_HOME:-~/.codex}/vantage/outbox`. One detached sender per connection
retries delivery. Session and turn lifecycle events trigger incremental
recovery from Codex rollouts, including eligible threads created after setup
whose hooks never fired. Billing continues to use native usage.

## Measuring the outbox

From the same Codex home, inspect queued evidence or request recovery:

```sh
python3 -I "${CODEX_HOME:-$HOME/.codex}/vantage/hooks/run_vantage_codex_hook.py" status
python3 -I "${CODEX_HOME:-$HOME/.codex}/vantage/hooks/run_vantage_codex_hook.py" recover
```

Run the `status` command above before a workload, while several agents are
active, and again after Stop. Record the time along with `outbox`,
`pending_recoveries`, `last_ack_unix_seconds`, `consecutive_send_failures`,
`retry_in_seconds`, and the cumulative `rollout_io_errors` (absent means zero).
The outbox counts unacknowledged events. A backlog that grows across turns
while acknowledgements advance indicates the sender is falling behind.
A stalled acknowledgement with increasing failures indicates a delivery
problem. Record the readings before using manual recovery, which retries
immediately. SQLite file size also includes retained session state.

Hooks share one short-lived sender per credential. It sends up to 64 rows at
a time, coalesces recovery requests, and releases ownership when idle. Failed
exports keep their event IDs and set a bounded retry delay. A later hook or
manual recovery wakes the sender. No periodic process runs while Codex is idle.
On session start or resume the hook refreshes the installed CLI version if
the hook payload omits it. Rollout recovery uses timestamped observations.
