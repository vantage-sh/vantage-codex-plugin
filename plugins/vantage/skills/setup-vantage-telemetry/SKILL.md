---
name: setup-vantage-telemetry
description: Configure, check, update, or remove Codex OpenTelemetry usage log export to Vantage.
---

# Set up Vantage telemetry

Call the bundled `connect_vantage_telemetry` tool. If its status is `pending`, call `check_vantage_telemetry_setup` up to three times. Report a final status immediately. If setup is still pending after three checks, ask the user to finish browser sign-in and reply `check Vantage setup`; do not promise that the background process will remain available.

When the user asks to check Vantage setup, call `check_vantage_telemetry_setup` first. If it returns `not_started`, explain that the previous local setup process stopped or expired and call `connect_vantage_telemetry` to start again. If it returns `pending`, check up to three times before handing control back in the same way.

If a status is `missing_requirements`, tell the user setup did not start, list each item from the tool's message with how to install it, and ask them to reply `set up Vantage telemetry` once they have. Do not install software yourself unless the user asks you to.

If setup succeeds with a Git warning, report the successful connection and explain how installing Git enables repository attribution.

If the Vantage setup tools are unavailable or report that Python is required, tell the user to install Python 3.9 or newer with SQLite support and make the `python3` command available. On Windows, suggest the Python Install Manager from python.org. Ask them to restart Codex and retry setup.

Do not run setup scripts directly, construct authorization URLs, or request or expose credentials.

Only when the user explicitly asks to remove Vantage telemetry, call the bundled `remove_vantage_telemetry` tool and report its result.

## Automatic attribution

After connecting, explain that `set_turn_attribution` saves the classifier tags for a turn. On its first approval prompt, selecting **Always allow** remembers the choice for this plugin and tool across sessions. For headless use, offer to configure the same setting in the user's Codex config. With their consent, use the installed plugin selector in place of `vantage@vantage-codex-plugin` below. Preserve existing config and restart Codex after editing.

```toml
[plugins."vantage@vantage-codex-plugin".mcp_servers.vantage_local.tools.set_turn_attribution]
approval_mode = "approve"
```

In a Git checkout, determine the hostname of the origin fetch URL without displaying credentials. Use `gh auth status --active --json hosts --jq '.hosts[][] | {host,login}'` to find the active login for that exact host. Offer it as the GitHub handle for attribution and show the repository or host scope before saving. If no account matches, offer to use the handle the user confirms in Vantage's GitHub integration, or skip. Never infer a personal login from the repository owner or Git author name.

With the user's confirmation, store the handle using `git config --local "vantage.<hostname>.githubLogin" "<login>"` in the checkout. If they choose a default for all repositories on this host, use `--global` instead. Repository settings override the host default for people using multiple accounts. Use validated host and login values as literal arguments. The hook reads the setting locally and never needs a GitHub token.
