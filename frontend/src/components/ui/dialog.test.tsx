import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from './dialog'

function TestDialog({ onOpenChange }: { onOpenChange?: (open: boolean) => void }) {
  return (
    <Dialog onOpenChange={onOpenChange}>
      <DialogTrigger>Open settings</DialogTrigger>
      <DialogContent>
        <DialogTitle>Settings</DialogTitle>
        <DialogDescription>Adjust your preferences.</DialogDescription>
        <button>Save</button>
      </DialogContent>
    </Dialog>
  )
}

describe('Dialog', () => {
  it('is closed until the trigger is clicked', () => {
    render(<TestDialog />)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('opens on trigger click and renders its title/content', async () => {
    render(<TestDialog />)
    await userEvent.click(screen.getByRole('button', { name: 'Open settings' }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText('Settings')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument()
  })

  it('closes on Escape', async () => {
    const onOpenChange = vi.fn()
    render(<TestDialog onOpenChange={onOpenChange} />)
    await userEvent.click(screen.getByRole('button', { name: 'Open settings' }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    await userEvent.keyboard('{Escape}')
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('closes when the close button is clicked', async () => {
    render(<TestDialog />)
    await userEvent.click(screen.getByRole('button', { name: 'Open settings' }))
    await userEvent.click(screen.getByRole('button', { name: 'Close dialog' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  // Regression coverage for the 2026-09-19 dark-mode background-bleed-through
  // bug (docs/settings-modal-visual-bug.md): the backdrop used to be
  // `bg-black/40` (40% opaque -- page content visible through it) and the
  // panel used `bg-[var(--card)]`, which carries a translucent "glass" alpha
  // channel in dark mode (`--card: #15132485` in index.css) -- so the panel
  // itself let background content bleed through too. Both must now use the
  // dedicated, always-fully-opaque `--modal-backdrop`/`--modal-surface`
  // tokens instead.
  it('renders a fully opaque backdrop, never a translucent bg-black overlay', async () => {
    render(<TestDialog />)
    await userEvent.click(screen.getByRole('button', { name: 'Open settings' }))

    // Tailwind arbitrary-value classes (`bg-[var(--modal-backdrop)]`) contain
    // characters CSS attribute/class selectors can't safely target, so this
    // walks every rendered element and matches on the literal className
    // string instead of a querySelector.
    const overlay = Array.from(document.body.querySelectorAll('*')).find((el) =>
      el.className.toString().includes('modal-backdrop'),
    )
    expect(overlay).toBeTruthy()
    expect(overlay?.className.toString()).not.toMatch(/bg-black/)
  })

  it('renders a fully opaque dialog panel, never the translucent --card token', async () => {
    render(<TestDialog />)
    await userEvent.click(screen.getByRole('button', { name: 'Open settings' }))

    const dialog = screen.getByRole('dialog')
    expect(dialog.className).toContain('bg-[var(--modal-surface)]')
    expect(dialog.className).not.toMatch(/bg-\[var\(--card\)\]/)
  })

  it('never renders more than one dialog panel at a time', async () => {
    render(<TestDialog />)
    await userEvent.click(screen.getByRole('button', { name: 'Open settings' }))
    expect(screen.getAllByRole('dialog')).toHaveLength(1)
  })
})
