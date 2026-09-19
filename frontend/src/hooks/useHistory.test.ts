import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { useHistory } from './useHistory'

describe('useHistory', () => {
  it('starts with the initial value and no undo/redo available', () => {
    const { result } = renderHook(() => useHistory(0))
    expect(result.current.state).toBe(0)
    expect(result.current.canUndo).toBe(false)
    expect(result.current.canRedo).toBe(false)
  })

  it('set() updates state and enables undo', () => {
    const { result } = renderHook(() => useHistory(0))
    act(() => result.current.set(1))
    expect(result.current.state).toBe(1)
    expect(result.current.canUndo).toBe(true)
    expect(result.current.canRedo).toBe(false)
  })

  it('undo reverts to the previous value and enables redo', () => {
    const { result } = renderHook(() => useHistory(0))
    act(() => result.current.set(1))
    act(() => result.current.undo())
    expect(result.current.state).toBe(0)
    expect(result.current.canUndo).toBe(false)
    expect(result.current.canRedo).toBe(true)
  })

  it('redo re-applies an undone value', () => {
    const { result } = renderHook(() => useHistory(0))
    act(() => result.current.set(1))
    act(() => result.current.undo())
    act(() => result.current.redo())
    expect(result.current.state).toBe(1)
    expect(result.current.canRedo).toBe(false)
  })

  it('a new set() after undo discards the redo-able future', () => {
    const { result } = renderHook(() => useHistory(0))
    act(() => result.current.set(1))
    act(() => result.current.set(2))
    act(() => result.current.undo())
    expect(result.current.state).toBe(1)
    act(() => result.current.set(99))
    expect(result.current.state).toBe(99)
    expect(result.current.canRedo).toBe(false)
  })

  it('undo/redo are no-ops at the boundaries', () => {
    const { result } = renderHook(() => useHistory(0))
    act(() => result.current.undo())
    expect(result.current.state).toBe(0)
    act(() => result.current.redo())
    expect(result.current.state).toBe(0)
  })

  it('reset() clears both past and future', () => {
    const { result } = renderHook(() => useHistory(0))
    act(() => result.current.set(1))
    act(() => result.current.set(2))
    act(() => result.current.undo())
    act(() => result.current.reset(42))
    expect(result.current.state).toBe(42)
    expect(result.current.canUndo).toBe(false)
    expect(result.current.canRedo).toBe(false)
  })
})
