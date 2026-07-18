# Talos, an AI powered vulnerability detection and auto-remediation via MCP

> Talos is an AI-powered cybersecurity assistant and brute-force detection platform built for defensive security analysis, incident response, phishing/link safety checks, research discovery, security reporting, and practical remediation workflows.

![Model Context Protocol](https://img.shields.io/badge/Model%20Context%20Protocol-MCP-blue)
![Built with Nitrostack](https://img.shields.io/badge/Built%20with-Nitrostack-0A66FF)
![Status](https://img.shields.io/badge/status-live-brightgreen)

**Talos, an AI powered vulnerability detection and auto-remediation via MCP** is an [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that extends AI assistants with real defensive-security tools. It is built for NitroStack/NitroCloud deployment and can be connected to MCP-compatible clients such as Claude Desktop, Cursor, NitroChat, and other AI agent environments.

## Table of Contents

- [Overview](#overview)
- [What is MCP?](#what-is-mcp)
- [Features](#features)
- [Tools](#tools)
- [Live Demo](#live-demo)
- [Getting Started](#getting-started)
- [Connect to an MCP Client](#connect-to-an-mcp-client)
- [Deploy Your Own MCP App](#deploy-your-own-mcp-app)
- [Environment Variables](#environment-variables)
- [Explore More MCP Apps](#explore-more-mcp-apps)
- [FAQ](#faq)
- [Keywords](#keywords)
- [License](#license)

## Overview

Talos combines a TypeScript/NitroStack MCP server with a bundled standalone security runtime. It exposes structured tools for passive website scanning, CVE lookup, authentication-log analysis, research-paper discovery, phishing and malicious-link detection, password checks, JWT decoding, IP investigation, security report generation, email alerting, and defensive resource search.

Instead of giving generic text-only answers, an AI assistant can call the correct Talos MCP tool directly, inspect live results, generate evidence-backed reports, and move from detection to investigation and remediation in one workflow.

The project is designed for standalone NitroStack hosting. When Python is unavailable in the hosting runtime, Talos can run its Node-compatible fallback tools with `TALOS_FORCE_NODE_TOOLS=true`.

## What is MCP?

The **Model Context Protocol (MCP)** is an open standard that lets AI assistants securely connect to external tools, data sources, and services. Instead of being limited to static model knowledge, an AI assistant can call MCP servers to fetch live data, run actions, and integrate with real systems.

This project is one such MCP server. Learn more about building and shipping MCP apps at [nitrostack.ai](https://nitrostack.ai).

## Features

- **MCP-native** - works with MCP-compatible clients.
- **NitroStack ready** - deployable as a standalone NitroStack MCP app.
- **Security toolbox** - website scanning, CVE lookup, phishing/link safety, IP lookup, password utilities, JWT decoding, and more.
- **Research support** - academic/security research search and paper lookup tools.
- **Report generation** - creates structured security reports without emailing unless explicitly requested.
- **Email capable** - can send alerts or report summaries when SMTP/Gmail settings are configured.
- **Node-compatible fallback mode** - works in NitroCloud-style Node-only hosting.
- **Secret-safe repo** - API keys and mail credentials belong in environment variables, not in committed code.

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

## Live Demo

**Live MCP endpoint:**

```text
https://mentora-6a-mentora-builders-amrita-university-amritapuri-campus.app.nitrocloud.ai/mcp
```

Point your MCP client at the endpoint above to try it instantly. Prefer your own hosted setup? Deploy this repo on [NitroStack](https://nitrostack.ai).

## Getting Started

### Prerequisites

- Node.js 20+ for NitroStack builds.
- npm.
- An MCP-compatible client such as NitroChat, Claude Desktop, Cursor, or another MCP client.

### Installation

```bash
git clone https://github.com/arshabmohan03-hash/talos-mcp-server.git
cd talos-mcp-server
npm install
```

### Configuration

Copy the example environment file and add your own values:

```bash
cp .env.example .env
```

For NitroCloud/Node-only deployments, use:

```text
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

### Run Locally

```bash
npm run build
node dist/index.js
```

Health check:

```bash
curl http://localhost:3002/mcp/health
```

## Connect to an MCP Client

Add this server to your MCP client configuration:

```json
{
  "mcpServers": {
    "talos-mcp-server": {
      "url": "https://mentora-6a-mentora-builders-amrita-university-amritapuri-campus.app.nitrocloud.ai/mcp"
    }
  }
}
```

Restart your client and the Talos tools will be available to your AI assistant.

## Deploy Your Own MCP App

Upload this repository or connect it to NitroStack.

Recommended NitroStack settings:

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

Want to build and ship an MCP server like this one? [NitroStack](https://nitrostack.ai) lets you create, deploy, and host MCP apps without managing infrastructure.

## Environment Variables

Set these in your deployment environment as needed:

```text
GOOGLE_SAFE_BROWSING_API_KEY=
VIRUSTOTAL_API_KEY=
PHISHTANK_API_KEY=
URLSCAN_API_KEY=
CEREBRAS_API_KEY=
CEREBRAS_API_KEY_SECONDARY=
GROQ_API_KEYS=
OPENALEX_API_KEY=
CORE_API_KEY=
SEMANTIC_SCHOLAR_API_KEY=
ALERT_EMAIL=
ALERT_EMAIL_PASSWORD=
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SECURITY_ALERT_EMAIL=
SLACK_WEBHOOK_URL=
```

Do not commit `.env`, app passwords, API keys, Firebase service-account files, or local test-key files.

## Explore More MCP Apps

- Discover and share MCP projects with the community on [r/mcptothemoon](https://www.reddit.com/r/mcptothemoon/).
- Browse MCP apps and build your own on [NitroStack](https://nitrostack.ai).

## FAQ

### What is an MCP server?

An MCP server implements the Model Context Protocol to expose tools, resources, and prompts that AI assistants can call. It lets an AI model take useful actions and access live data through a structured interface.

### What does Talos do?

Talos provides defensive cybersecurity tools through MCP, including passive website security checks, brute-force log analysis, CVE lookup, phishing/link safety detection, security report generation, research lookup, and email alert workflows.

### Does report generation automatically send email?

No. `generate_security_report` returns and saves a report by default. Email is sent only when the user explicitly asks to email, forward, send, or share the report, or when an email-specific tool is called.

### Which AI clients does this work with?

Any MCP-compatible client that can connect to an HTTP MCP endpoint, including NitroChat, Claude Desktop, Cursor, and other agent environments.

### How do I deploy my own MCP app?

Use [NitroStack](https://nitrostack.ai) to build, deploy, and host MCP apps without managing server infrastructure.

## Keywords

`Talos` - `MCP` - `Model Context Protocol` - `MCP server` - `NitroStack` - `NitroCloud` - `AI security assistant` - `vulnerability detection` - `brute-force detection` - `phishing detection` - `link safety` - `security report` - `CVE lookup` - `cybersecurity tools` - `AI agents`

## License

MIT (C) 2026

---

Built with the Model Context Protocol on [NitroStack](https://nitrostack.ai). Share MCP apps with the community on [r/mcptothemoon](https://www.reddit.com/r/mcptothemoon/).
