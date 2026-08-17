/** Shared versioned JSON contracts for the Wren Data Agent. */

export * from './json.js'
export * from './errors.js'
export * from './schema.js'
export * from './rpc.js'
export * from './context.js'
export * from './chart.js'
export * from './query.js'

import type { RpcRequest, RpcResponse } from './rpc.js'
import type { SemanticContext } from './context.js'
import type { ChartSpecV1 } from './chart.js'
import type { DataQueryInput, DataQueryPresentation } from './query.js'
import { rpcRequestSchema, rpcResponseSchema } from './rpc.js'
import { semanticContextSchema } from './context.js'
import { chartSpecV1Schema } from './chart.js'
import { dataQueryInputSchema, dataQueryPresentationSchema } from './query.js'
import type { SafeParseResult } from './json.js'

/** Parse an RPC request without throwing on invalid input. */
export const safeParseRpcRequest = (value: unknown): SafeParseResult<RpcRequest> => rpcRequestSchema.safeParse(value)

/** Parse an RPC response without throwing on invalid input. */
export const safeParseRpcResponse = (value: unknown): SafeParseResult<RpcResponse> => rpcResponseSchema.safeParse(value)

/** Parse semantic context without throwing on invalid input. */
export const safeParseSemanticContext = (value: unknown): SafeParseResult<SemanticContext> => semanticContextSchema.safeParse(value)

/** Parse DataQuery input without throwing on invalid input. */
export const safeParseDataQueryInput = (value: unknown): SafeParseResult<DataQueryInput> => dataQueryInputSchema.safeParse(value)

/** Parse ChartSpecV1 without throwing on invalid input. */
export const safeParseChartSpecV1 = (value: unknown): SafeParseResult<ChartSpecV1> => chartSpecV1Schema.safeParse(value)

/** Parse DataQuery presentation without throwing on invalid input. */
export const safeParseDataQueryPresentation = (value: unknown): SafeParseResult<DataQueryPresentation> => dataQueryPresentationSchema.safeParse(value)
