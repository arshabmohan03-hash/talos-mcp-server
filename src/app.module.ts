import { McpApp, Module } from '@nitrostack/core';
import { TalosModule } from './modules/talos/talos.module.js';
import { SystemHealthCheck } from './health/system.health.js';

@McpApp({
  module: AppModule,
  server: {
    name: 'talos-mcp-server',
    version: '1.0.0',
  },
  logging: {
    level: 'info',
  },
})
@Module({
  name: 'app',
  description: 'Root application module',
  imports: [
    TalosModule,
  ],
  providers: [
    SystemHealthCheck,
  ],
})
export class AppModule {}
