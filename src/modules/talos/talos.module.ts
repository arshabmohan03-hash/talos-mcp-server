import { Module } from '@nitrostack/core';
import { TalosTools } from './talos.tools.js';
import { TalosResources } from './talos.resources.js';
import { TalosPrompts } from './talos.prompts.js';

@Module({
  name: 'talos',
  description: 'Talos AI security scanner, brute-force detector, and defensive toolbox',
  controllers: [TalosTools, TalosResources, TalosPrompts],
})
export class TalosModule {}
