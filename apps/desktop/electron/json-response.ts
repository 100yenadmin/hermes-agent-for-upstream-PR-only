import type { IncomingMessage, RequestOptions } from 'node:http'
import type http from 'node:http'
import { gunzipSync } from 'node:zlib'

export function decodeJsonResponseBody(chunks: Buffer[], encoding: string | string[] | undefined): string {
  const body = Buffer.concat(chunks)

  return (String(encoding).trim().toLowerCase() === 'gzip' ? gunzipSync(body) : body).toString('utf8')
}

export function decodeJsonResponse(
  chunks: Buffer[],
  encoding: string | string[] | undefined,
  reject: (error: Error) => void,
  onText: (text: string) => void
): void {
  try {
    onText(decodeJsonResponseBody(chunks, encoding))
  } catch (error) {
    reject(error as Error)
  }
}

// Chromium's electron.net already decodes responses; this is for Node HTTP only.
export function requestJsonResponse(
  client: Pick<typeof http, 'request'>,
  url: URL,
  options: RequestOptions,
  onResponse: (response: IncomingMessage) => void
) {
  return client.request(url, {
    ...options,
    headers: { ...options.headers, 'Accept-Encoding': 'gzip' }
  }, onResponse)
}
