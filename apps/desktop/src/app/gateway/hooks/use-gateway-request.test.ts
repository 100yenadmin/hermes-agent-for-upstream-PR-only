import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HermesGateway } from '@/hermes'
import { $gateway } from '@/store/gateway'
import { setGatewayState } from '@/store/session'

import { requestMayReplayAfterReconnect, useGatewayRequest } from './use-gateway-request'

const fakeGateway = { connectionState: 'open' } as unknown as HermesGateway

afterEach(() => {
  $gateway.set(null)
})

describe('useGatewayRequest', () => {
  // The composer's `/` completions only exist when ChatBar receives a non-null
  // gateway PROP. `gatewayRef` is populated by a subscription effect, so it is
  // still null on the first render — a surface that read the ref while
  // rendering (session tiles / ⌘T tabs) shipped `gateway={null}` and silently
  // lost slash completions. The returned `gateway` value must be live
  // immediately so that never happens again.
  it('exposes the live gateway on the first render, before effects run', () => {
    $gateway.set(fakeGateway)

    const { result } = renderHook(() => useGatewayRequest())

    expect(result.current.gateway).toBe(fakeGateway)
  })

  it('tracks the gateway when the active socket changes', () => {
    const { result } = renderHook(() => useGatewayRequest())

    expect(result.current.gateway).toBeNull()

    act(() => $gateway.set(fakeGateway))

    expect(result.current.gateway).toBe(fakeGateway)
  })

  it('never replays prompt.submit after an ambiguous reconnect', () => {
    expect(requestMayReplayAfterReconnect('prompt.submit')).toBe(false)
    expect(requestMayReplayAfterReconnect('session.list')).toBe(true)
    expect(requestMayReplayAfterReconnect('session.list', { replayOnReconnect: false })).toBe(false)
  })

  it('reconnects and reports prompt delivery as unconfirmed after a submit timeout', async () => {
    let connectionState = 'open'

    const request = vi.fn(async () => {
      throw new Error('request timed out after 30s: prompt.submit')
    })

    const invalidate = vi.fn(() => {
      connectionState = 'closed'
      setGatewayState('closed')
    })

    const connect = vi.fn(async () => {
      connectionState = 'open'
      setGatewayState('open')
    })

    const gateway = {
      connect,
      invalidate,
      request,
      get connectionState() {
        return connectionState
      }
    } as unknown as HermesGateway

    const desktop = window.hermesDesktop

    window.hermesDesktop = {
      ...desktop,
      getConnection: vi.fn(async () => ({
        authMode: 'token',
        baseUrl: 'http://gateway.test',
        isFullscreen: false,
        logs: [],
        nativeOverlayWidth: 0,
        token: 'test-token',
        windowButtonPosition: null,
        wsUrl: 'ws://gateway.test/api/ws'
      }))
    } as typeof window.hermesDesktop
    setGatewayState('open')
    $gateway.set(gateway)

    try {
      const { result } = renderHook(() => useGatewayRequest())

      await expect(result.current.requestGateway('prompt.submit', { text: 'one prompt' })).rejects.toThrow(
        'delivery not confirmed after reconnect: prompt.submit'
      )
      expect(request).toHaveBeenCalledTimes(1)
      expect(invalidate).toHaveBeenCalledTimes(1)
      expect(connect).toHaveBeenCalledTimes(1)
    } finally {
      window.hermesDesktop = desktop
    }
  })

  it('waits for an in-flight reconnect owner before reporting an ambiguous submit', async () => {
    let connectionState = 'open'
    let settled = false
    const stateHandlers = new Set<(state: string) => void>()

    const request = vi.fn(async () => {
      throw new Error('request timed out after 30s: prompt.submit')
    })

    const invalidate = vi.fn(() => {
      // The normal boot owner reacts first and starts the replacement socket.
      connectionState = 'connecting'
      setGatewayState('connecting')
    })

    // JsonRpcGatewayClient.connect() returns immediately when another owner is
    // already connecting. requestGateway must still wait for the real open.
    const connect = vi.fn(async () => undefined)

    const gateway = {
      connect,
      invalidate,
      onState: vi.fn((handler: (state: string) => void) => {
        stateHandlers.add(handler)

        return () => stateHandlers.delete(handler)
      }),
      request,
      get connectionState() {
        return connectionState
      }
    } as unknown as HermesGateway

    const desktop = window.hermesDesktop

    window.hermesDesktop = {
      ...desktop,
      getConnection: vi.fn(async () => ({
        authMode: 'token',
        baseUrl: 'http://gateway.test',
        isFullscreen: false,
        logs: [],
        nativeOverlayWidth: 0,
        token: 'test-token',
        windowButtonPosition: null,
        wsUrl: 'ws://gateway.test/api/ws'
      }))
    } as typeof window.hermesDesktop
    setGatewayState('open')
    $gateway.set(gateway)

    try {
      const { result } = renderHook(() => useGatewayRequest())
      const recovery = result.current.requestGateway('prompt.submit', { text: 'one prompt' })

      void recovery.then(
        () => {
          settled = true
        },
        () => {
          settled = true
        }
      )

      await vi.waitFor(() => expect(connect).toHaveBeenCalledTimes(1))
      expect(settled).toBe(false)

      connectionState = 'open'
      setGatewayState('open')
      stateHandlers.forEach(handler => handler('open'))

      await expect(recovery).rejects.toThrow('delivery not confirmed after reconnect: prompt.submit')
      expect(request).toHaveBeenCalledTimes(1)
    } finally {
      window.hermesDesktop = desktop
    }
  })
})
