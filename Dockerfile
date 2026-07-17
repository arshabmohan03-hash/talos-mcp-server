# Build with talos-mcp-server as Docker context:
# npm run prepare:standalone
# docker build -t talos-mcp-server .

FROM node:20-bookworm-slim AS node-build
WORKDIR /app/talos-mcp-server
COPY package*.json ./
RUN npm ci
COPY . ./
RUN npm run build

FROM node:20-bookworm-slim AS runtime
RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates dnsutils openssl \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app/talos-mcp-server
COPY package*.json ./
RUN npm ci --omit=dev
COPY --from=node-build /app/talos-mcp-server/dist ./dist
COPY bridge ./bridge
COPY src/widgets ./src/widgets
COPY app-runtime /app/app-runtime

RUN python3 -m venv /opt/talos-venv \
  && /opt/talos-venv/bin/pip install --no-cache-dir --upgrade pip \
  && /opt/talos-venv/bin/pip install --no-cache-dir -r /app/app-runtime/requirements.txt

ENV NODE_ENV=production \
  PORT=3002 \
  MCP_HOST=0.0.0.0 \
  MCP_BASE_PATH=/mcp \
  MCP_TRANSPORT=dual \
  MCP_TRANSPORT_TYPE=dual \
  HOST=0.0.0.0 \
  TALOS_APP_ROOT=/app/app-runtime \
  TALOS_PYTHON=/opt/talos-venv/bin/python \
  TALOS_TOOL_TIMEOUT_MS=60000

EXPOSE 3002
CMD ["node", "dist/index.js"]
