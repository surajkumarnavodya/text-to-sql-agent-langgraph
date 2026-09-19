import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '@/components/ui/toast'
import { useImageAttachments } from './useImageAttachments'

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]

function makePngFile(name = 'photo.png'): File {
  return new File([new Uint8Array(PNG_SIGNATURE)], name, { type: 'image/png' })
}

function wrapper({ children }: { children: ReactNode }) {
  return <ToastProvider>{children}</ToastProvider>
}

describe('useImageAttachments', () => {
  beforeEach(() => {
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn(() => 'blob:mock'), revokeObjectURL: vi.fn() })
    class FakeImage {
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      naturalWidth = 100
      naturalHeight = 100
      set src(_value: string) {
        queueMicrotask(() => this.onload?.())
      }
    }
    vi.stubGlobal('Image', FakeImage)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('starts empty', () => {
    const { result } = renderHook(() => useImageAttachments(), { wrapper })
    expect(result.current.images).toHaveLength(0)
    expect(result.current.canAddMore).toBe(true)
  })

  it('adds a valid image file', async () => {
    const { result } = renderHook(() => useImageAttachments(), { wrapper })
    await act(async () => {
      await result.current.addFiles([makePngFile()])
    })
    expect(result.current.images).toHaveLength(1)
    expect(result.current.images[0].file.name).toBe('photo.png')
    expect(result.current.images[0].editedDataUrl).toBeNull()
  })

  it('removeImage removes it from the list', async () => {
    const { result } = renderHook(() => useImageAttachments(), { wrapper })
    await act(async () => {
      await result.current.addFiles([makePngFile()])
    })
    const id = result.current.images[0].id
    act(() => result.current.removeImage(id))
    expect(result.current.images).toHaveLength(0)
  })

  it('setEditedImage records the edited data URL without touching the original', async () => {
    const { result } = renderHook(() => useImageAttachments(), { wrapper })
    await act(async () => {
      await result.current.addFiles([makePngFile()])
    })
    const id = result.current.images[0].id
    const originalUrl = result.current.images[0].originalUrl
    act(() => result.current.setEditedImage(id, 'data:image/png;base64,edited'))
    expect(result.current.images[0].editedDataUrl).toBe('data:image/png;base64,edited')
    expect(result.current.images[0].originalUrl).toBe(originalUrl)
  })

  it('refuses to add more than MAX_ATTACHED_IMAGES', async () => {
    const { result } = renderHook(() => useImageAttachments(), { wrapper })
    await act(async () => {
      await result.current.addFiles([
        makePngFile('a.png'),
        makePngFile('b.png'),
        makePngFile('c.png'),
        makePngFile('d.png'),
        makePngFile('e.png'),
      ])
    })
    expect(result.current.images).toHaveLength(4) // MAX_ATTACHED_IMAGES
    expect(result.current.canAddMore).toBe(false)
  })

  it('clearAll empties the list', async () => {
    const { result } = renderHook(() => useImageAttachments(), { wrapper })
    await act(async () => {
      await result.current.addFiles([makePngFile()])
    })
    act(() => result.current.clearAll())
    expect(result.current.images).toHaveLength(0)
  })
})
