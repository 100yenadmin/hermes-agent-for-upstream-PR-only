import type { IncomingMessage, RequestOptions } from 'node:http'
import type http from 'node:http'
import { gunzipSync } from 'node:zlib'

// Over loopback the transfer costs nothing, so compressing would only spend server CPU.
const LOOPBACK_HOSTNAMES = new Set(['localhost', '127.0.0.1', '[::1]'])

export function asksForGzip(url: URL): boolean {
  const host = url.hostname.toLowerCase()

  return !(LOOPBACK_HOSTNAMES.has(host) || host.startsWith('127.') || host.endsWith('.localhost'))
}

export function decodeJsonResponseBody(chunks: Buffer[], encoding: string | string[] | undefined): string {
  const body = Buffer.concat(chunks)
  // An empty body (a proxy's 204/304) has nothing to gunzip; keep its status for the caller.
  const gzipped = body.length > 0 && String(encoding).trim().toLowerCase() === 'gzip'

  return (gzipped ? gunzipSync(body) : body).toString('utf8')
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
  const headers = asksForGzip(url) ? { ...options.headers, 'Accept-Encoding': 'gzip' } : options.headers

  return client.request(url, { ...options, headers }, onResponse)
}
