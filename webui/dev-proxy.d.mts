import type { IncomingMessage, ServerResponse } from 'node:http'
export default function createAiMiddleware(configPath: string):
  (request: IncomingMessage, response: ServerResponse, next: () => void) => void
