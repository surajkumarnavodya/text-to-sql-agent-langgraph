import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ToastProvider, useToast } from './toast'

function ToastTrigger() {
  const { toast } = useToast()
  return (
    <button
      onClick={() =>
        toast({ title: 'Schema refreshed', description: '12 tables indexed.', variant: 'success' })
      }
    >
      Trigger
    </button>
  )
}

function ErrorToastTrigger() {
  const { toast } = useToast()
  return (
    <button onClick={() => toast({ title: 'Upload failed', variant: 'error', durationMs: 0 })}>
      Trigger error
    </button>
  )
}

describe('ToastProvider / useToast', () => {
  it('renders a toast with role=status for a success/info toast', async () => {
    render(
      <ToastProvider>
        <ToastTrigger />
      </ToastProvider>,
    )
    await userEvent.click(screen.getByRole('button', { name: 'Trigger' }))
    const toast = screen.getByRole('status')
    expect(toast).toHaveTextContent('Schema refreshed')
    expect(toast).toHaveTextContent('12 tables indexed.')
  })

  it('renders a toast with role=alert for an error toast', async () => {
    render(
      <ToastProvider>
        <ErrorToastTrigger />
      </ToastProvider>,
    )
    await userEvent.click(screen.getByRole('button', { name: 'Trigger error' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Upload failed')
  })

  it('dismisses a toast when its close button is clicked', async () => {
    render(
      <ToastProvider>
        <ErrorToastTrigger />
      </ToastProvider>,
    )
    await userEvent.click(screen.getByRole('button', { name: 'Trigger error' }))
    expect(screen.getByRole('alert')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Dismiss notification' }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('throws a clear error when useToast is called outside a provider', () => {
    // Swallow the expected React error-boundary console noise for this one
    // assertion -- render() itself is what throws here since there's no
    // error boundary in the test tree, not an uncaught async rejection.
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() => render(<ToastTrigger />)).toThrow(/useToast must be used within/)
    spy.mockRestore()
  })
})
