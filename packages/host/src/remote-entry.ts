/** Thin DeepSeek Harness adapter for a separately running SemaRail Core. */

import type { Context } from '@deepseek-ai/cordis'
import {
  SYSTEM_PROMPT_GUIDANCE,
  SYSTEM_PROMPT_SECTION_NAME,
  installDataQueryTool,
  installSemanticContextTool,
  unavailableQueryGateway,
  unavailableSemanticContextGateway,
} from './tooling.js'
import { createCoreHttpGateway, type CoreHttpGatewayConfig } from './core-http.js'

export const name = 'semarail-harness-host'
export const inject = ['tools', 'systemPrompt'] as const

export interface SemaRailHarnessConfig extends CoreHttpGatewayConfig {}

export function apply(ctx: Context, config: SemaRailHarnessConfig = {}): void {
  let gateway: ReturnType<typeof createCoreHttpGateway> | undefined
  try {
    gateway = createCoreHttpGateway(config)
  } catch {
    gateway = undefined
  }
  if (gateway === undefined) {
    installDataQueryTool(ctx, unavailableQueryGateway)
    installSemanticContextTool(ctx, unavailableSemanticContextGateway)
  } else {
    installDataQueryTool(ctx, gateway)
    installSemanticContextTool(ctx, gateway)
    ctx.effect(() => () => gateway?.dispose(), 'semarail-harness-host.core-http()')
    void gateway.start().catch(() => undefined)
  }
  ctx.effect(
    () => ctx.systemPrompt.section({
      name: SYSTEM_PROMPT_SECTION_NAME,
      order: 125,
      text: SYSTEM_PROMPT_GUIDANCE,
    }),
    'semarail-harness-host.system-prompt',
  )
}
