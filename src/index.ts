import 'dotenv/config';
import { McpApplicationFactory } from '@nitrostack/core';
import { AppModule } from './app.module.js';

type RouteCapableServer = {
  getHttpTransport?: () => {
    getApp?: () => {
      get: (path: string, handler: (req: unknown, res: {
        status: (code: number) => { json: (body: unknown) => void };
      }) => void) => void;
    };
  } | undefined;
};

function registerDashboardHealthAliases(server: RouteCapableServer) {
  const app = server.getHttpTransport?.()?.getApp?.();
  if (!app) {
    return;
  }
  const handler = (_req: unknown, res: { status: (code: number) => { json: (body: unknown) => void } }) => {
    res.status(200).json({
      status: 'ok',
      service: 'talos-mcp-server',
      transport: process.env.MCP_TRANSPORT || process.env.MCP_TRANSPORT_TYPE || 'stdio',
      mcp_health: `${process.env.MCP_BASE_PATH || '/mcp'}/health`,
      timestamp: new Date().toISOString(),
    });
  };
  app.get('/health', handler);
  app.get('/healthz', handler);
  app.get('/ready', handler);
}

async function bootstrap() {
  const transport = process.env.MCP_TRANSPORT || process.env.MCP_TRANSPORT_TYPE
    || (process.env.NODE_ENV === 'production' ? 'dual' : 'stdio');
  const port = Number.parseInt(process.env.PORT || process.env.MCP_PORT || '3002', 10);
  const host = process.env.MCP_HOST || '0.0.0.0';

  process.env.MCP_TRANSPORT = process.env.MCP_TRANSPORT || transport;
  process.env.MCP_TRANSPORT_TYPE = process.env.MCP_TRANSPORT_TYPE || transport;
  process.env.PORT = String(port);
  process.env.HOST = process.env.HOST || host;

  const server = await McpApplicationFactory.create(AppModule);
  await server.start();
  registerDashboardHealthAliases(server as RouteCapableServer);
}

bootstrap().catch((error) => {
  console.error('Failed to start Talos MCP server:', error);
  process.exit(1);
});
