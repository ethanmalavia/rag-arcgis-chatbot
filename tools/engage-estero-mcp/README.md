# engage-estero-sync MCP

Coordination MCP for Nolan / Ethan / Krish Cursor agents.

## Local (stdio)

```bash
.venv/bin/pip install -r tools/engage-estero-mcp/requirements.txt
# or: pip install -r backend/requirements.txt
```

Cursor loads `.cursor/mcp.json` → **engage-estero-sync**.

## VPS (Streamable HTTP)

Deployed with the main API container (`ENABLE_MCP_HTTP=true`):

| | |
| --- | --- |
| URL | `https://tracker.esterotoday.com/mcp` |
| Auth | `Authorization: Bearer <MCP_API_KEY>` (defaults to `ADMIN_API_KEY`) |
| Transport | Streamable HTTP, `stateless_http=True` |

Cursor remote entry — copy [`.cursor/mcp.cloud.example.json`](../../.cursor/mcp.cloud.example.json)
into `.cursor/mcp.json`, then:

```bash
export ENGAGE_MCP_URL="https://tracker.esterotoday.com/mcp"
export ENGAGE_MCP_TOKEN="your-admin-or-mcp-key"
```

## Tools

| Tool | Purpose |
| --- | --- |
| `get_session_brief` | **Session start** — compact focus, priorities, claims, blockers, latest handoff excerpt |
| `get_shared_context` | Full context JSON (prefer brief for startup) |
| `update_shared_context` | Patch focus / priorities / blockers |
| `get_conventions` | Team process + coding rules (on demand) |
| `list_handoffs` | Recent handoff excerpts (default 3) |
| `get_handoff` | Full body of one handoff file |
| `write_handoff` | Leave a note for the next agent |
| `claim_area` / `release_area` | Soft locks |

State: `agent-sync/` in git (seed). On the VPS, copied to `/tmp/agent-sync` for writes (ephemeral per container — commit seed updates for durable sync).
