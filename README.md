# Talos MCP Server

NitroStack MCP server for the Talos AI Security Assistant. It exposes the
existing Python scanner, brute-force detector, security utilities, research
search, resource library, alerting, and mitigation generator as MCP tools.

## Tools

- `scan_website`
- `lookup_cves`
- `analyze_auth_log`
- `search_research`
- `find_research_papers`
- `analyze_link_safety`
- `generate_blocklist`
- `send_alert`
- `send_email`
- `generate_security_report`
- `send_report_email`
- `self_test_all_tools`
- `check_password_strength`
- `generate_password`
- `hash_text`
- `decode_jwt`
- `lookup_ip`
- `get_defense_status`
- `list_security_tools`
- `run_security_tool`
- `search_resources`
- `get_resource_page`
- `list_resources`

`generate_security_report` returns and saves a report by default. It does not
email anything unless `send_email=true` is explicitly passed. Use
`send_report_email` or `send_email` only when the user asks to email, send,
forward, or share the information.

## Resources and Prompts

Resources:

- `talos://overview`
- `talos://security-tools`
- `talos://resources`
- `talos://last-report`

Prompts:

- `talos_website_security_review`
- `talos_full_security_report`
- `talos_research_paper_lookup`
- `talos_link_safety_check`
- `talos_bruteforce_incident`
- `talos_tool_health_check`

## Local Setup

From the repository root, install the Python app dependencies first:

```powershell
pip install -r requirements.txt
```

Then install and run the MCP server:

```powershell
cd talos-mcp-server
npm install
Copy-Item .env.example .env
npm run dev
```

The server defaults to NitroStack dual transport with HTTP on port `3002` and
base path `/mcp`.

```powershell
curl http://localhost:3002/mcp/health
```

## Upload only `talos-mcp-server`

If your deploy UI only accepts this MCP folder, generate the embedded app
snapshot first:

```powershell
cd C:\Users\ignat\Downloads\AI_BruteForce_Detector-3\talos-mcp-server
npm run prepare:standalone
```

Then upload `talos-mcp-server` and use:

```text
Build command:
npm install
npm run build

Runtime:
node dist/index.js

Environment:
NODE_ENV=production
PORT=3002
MCP_TRANSPORT=dual
MCP_TRANSPORT_TYPE=dual
MCP_HOST=0.0.0.0
HOST=0.0.0.0
MCP_BASE_PATH=/mcp
TALOS_APP_ROOT=./app-runtime
TALOS_FORCE_NODE_TOOLS=true
TALOS_TOOL_TIMEOUT_MS=60000
```

`TALOS_FORCE_NODE_TOOLS=true` is recommended for NitroCloud-style Node-only
deployments. It avoids calling `python` and uses the TypeScript fallback tools
for research search, resource-library search, passive website/header scans,
CVEs, IP lookup, Gmail SMTP email sending, hashing, JWT decode, passwords,
blocklist generation, research-paper lookup, phishing/link-safety detection,
security-report generation, self-testing, and the small built-in security-tool
catalog. Local/full deployments can omit it to use the original Python bridge
for Python-backed tools; Node-only report, link-safety, and email tools still
route through TypeScript.

Do not upload local `.env` or `key.json`; set real secrets in the deployment
environment.

Do not commit or upload embedded test keys. Keep API keys, Gmail/SMTP app
passwords, Firebase credentials, and threat-intel provider keys in deployment
environment variables only.

`analyze_link_safety` uses static URL checks, DNS, redirects, lightweight HTTP
inspection, URLhaus, and configured threat-intel providers. Configure
`GOOGLE_SAFE_BROWSING_API_KEY`, `VIRUSTOTAL_API_KEY`, `PHISHTANK_API_KEY`, and
`URLSCAN_API_KEY` in the deployment environment when you want provider-backed
lookups.

## Nitro Studio

This folder is standalone for Nitro Studio. Open the folder itself:

```text
C:\Users\ignat\Downloads\AI_BruteForce_Detector-3\talos-mcp-server
```

Studio uses `STDIO` in development. Leave `MCP_TRANSPORT` and
`MCP_TRANSPORT_TYPE` unset for Studio so the server starts as pure stdio. For
hosted/HTTP deployment, set them to `dual` as shown above.

If Studio was already showing "Connection not found", close that failed project
entry and open this folder again after running:

```powershell
cd C:\Users\ignat\Downloads\AI_BruteForce_Detector-3\talos-mcp-server
npm install
npm run prepare:standalone
npm run build
```

## Upload the whole project

If your deploy UI accepts the full `AI_BruteForce_Detector-3` folder, use that
as the project root instead:

```text
Project root:
C:\Users\ignat\Downloads\AI_BruteForce_Detector-3

Build command:
npm install
npm run build:mcp-deploy

Runtime:
node talos-mcp-server/dist/index.js

Environment:
NODE_ENV=production
PORT=3002
MCP_TRANSPORT=dual
MCP_TRANSPORT_TYPE=dual
MCP_HOST=0.0.0.0
HOST=0.0.0.0
MCP_BASE_PATH=/mcp
TALOS_APP_ROOT=.
TALOS_PYTHON=python
TALOS_TOOL_TIMEOUT_MS=60000
```

## Environment

The TypeScript MCP server reads `talos-mcp-server/.env`. The Python Talos app
continues to read the root `.env` and the normal app settings.

Important MCP variables:

```text
PORT=3002
MCP_HOST=0.0.0.0
MCP_BASE_PATH=/mcp
MCP_TRANSPORT=dual
TALOS_APP_ROOT=./app-runtime
TALOS_TOOL_TIMEOUT_MS=60000
```

For NitroCloud/Node-only hosting, add:

```text
TALOS_FORCE_NODE_TOOLS=true
```

For local/full hosting with Python installed, omit `TALOS_FORCE_NODE_TOOLS` and
optionally set `TALOS_PYTHON=python` or `TALOS_PYTHON=python3`.

## NitroCloud / NitroStack Deploy

```powershell
npm install
npm run build
nitrostack login
nitrostack deploy
```

Set deployment secrets/env vars for the Talos Python app as needed:
`CEREBRAS_API_KEY`, alert settings, Firebase settings, scanner settings, and
any research API keys.

If the hosting runtime has no Python executable, keep:

```text
TALOS_FORCE_NODE_TOOLS=true
TALOS_APP_ROOT=./app-runtime
```

If you build on a platform where Python is optional, `npm run build:deploy` will
continue when no Python executable is found. Set `TALOS_REQUIRE_PYTHON=1` when
you want that build to fail instead.

## Docker

Prepare the standalone snapshot, then build from this folder:

```powershell
npm run prepare:standalone
docker build -t talos-mcp-server .
docker run --env-file .env -p 3002:3002 talos-mcp-server
```

## Safety

Talos tools are intended for defensive, authorized use. The website scanner is
passive/non-destructive, and `generate_blocklist` only returns rules for review;
it does not apply firewall changes.
