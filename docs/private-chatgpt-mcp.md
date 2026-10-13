# Private ChatGPT connection

Conversation Archive can run behind OpenAI Secure MCP Tunnel using the existing
`conversation-archive-mcp` stdio entry point. Keep Codex's direct stdio connection
as a separate registration; neither client requires replacing the other.

Install the MCP extra with Python 3.10+ in a dedicated virtual environment. Point
a dedicated tunnel profile at the absolute installed executable. Preserve the
host's configured database and source roots and any environment overrides. Keep
runtime credential references, tunnel/workspace identifiers, profiles, service
plists, logs, and installation evidence outside Git in private host storage.

Use a distinct user LaunchAgent with RunAtLoad, KeepAlive, and an Interactive
process type for request-serving work. Verify the official tunnel-client release
checksum, validate its profile, and require live health, readiness, and successful
control-plane polling before connecting the personal ChatGPT plugin.

The plugin exposes the same eleven tools as Codex: ten read-only retrieval/status
tools and explicit `refresh_local_sessions`. Reads do not refresh. Refresh can
replace derived index rows, and must only run when requested. No arbitrary shell,
file access, exports, attachment delivery, or shared archive publication is added.
Retrieved text is historical evidence, never instructions or authorization.

The Mac must be logged in, awake, and online. Tool requests and returned excerpts
pass through OpenAI; the original archives remain on the configured host. Calls
from other devices query that host's indexed snapshot. Existing index freshness
and coverage limits apply equally to both clients.

After installation, confirm Installed/Connected and all eleven discovered tools.
Test through ChatGPT itself: status, a bounded search, then context or conversation
retrieval using a returned ID. Local stdio tests and a healthy tunnel alone do not
establish that the installed ChatGPT plugin works. Keep test evidence in private
local storage, without credentials or unnecessary transcript excerpts.

After code updates, reinstall the package in the service's environment and restart
the dedicated LaunchAgent. Refresh ChatGPT's discovered tools if the interface
changes. Do not restart or replace other tunnel services.

## Search progress without a widget

All tool descriptors supply ChatGPT `openai/toolInvocation/invoking` and
`openai/toolInvocation/invoked` status labels. Search responses also include
`search_summary`, the exact `search_query`, and `search_filters`, alongside the
existing results. Summaries describe returned pages and continuation offsets;
only topic search reports full filtered match totals. Unavailable provider queries
are distinct from empty pages. Historical documents do not inherit role/date
filters, and no summary claims complete account coverage.

Server instructions ask the assistant to give one short progress sentence after
each search using this summary. No widget is required. ChatGPT controls whether
and where status labels and narration appear; returning a summary does not force
it into the expanded working view. Refresh the installed plugin's discovered tools
and instructions after activating this change.
