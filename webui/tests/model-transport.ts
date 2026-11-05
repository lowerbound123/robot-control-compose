import { requestDecision, type ApiConfig, type TranslationProgress } from '../src/lib/translator'
import { discoverCapabilities } from '../src/lib/agent-capabilities'
import { fixtureGateway } from './agent-fixtures'

// Transport cases use a fixed valid catalog; catalog refresh and planning have
// separate integration tests. No legacy translator exists in application code.
const snapshot = await discoverCapabilities(fixtureGateway())
export function translate(config: ApiConfig, input: string, _context: unknown, _policies: string[], signal?: AbortSignal,
  fetcher?: (url: string, init: RequestInit) => Promise<Response>, onProgress?: (value: TranslationProgress) => void) {
  return requestDecision(config, { input, snapshot, history: [], exchanges: [] }, { signal, fetcher, onProgress })
}
