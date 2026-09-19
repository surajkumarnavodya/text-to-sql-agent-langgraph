import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { AppShell } from './AppShell'

// Sidebar/MobileNav pull in chatStore/localAuthStore and a fair amount of
// their own UI -- irrelevant to what this file actually verifies (that the
// app shell's background content is marked `inert` while Settings is open),
// so they're stubbed to keep this test focused and fast. UserMenu and
// SettingsDialog are stubbed too, replaced with a minimal harness that lets
// the test trigger the exact same `onOpenSettings`/`open` wiring AppShell
// itself owns, without needing real auth/query-client context.
vi.mock('./Sidebar', () => ({ Sidebar: () => <div>Sidebar stub</div> }))
vi.mock('./MobileNav', () => ({ MobileNav: () => <div>MobileNav stub</div> }))
vi.mock('./UserMenu', () => ({
  UserMenu: ({ onOpenSettings }: { onOpenSettings: () => void }) => (
    <button onClick={onOpenSettings}>Open settings</button>
  ),
}))
vi.mock('./SettingsDialog', () => ({
  SettingsDialog: ({ open }: { open: boolean }) =>
    open ? <div role="dialog">Settings stub</div> : null,
}))

function renderAppShell() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<AppShell />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('AppShell background inert-while-modal-open', () => {
  it('does not mark the background content inert before Settings opens', () => {
    renderAppShell()
    const background = screen.getByText('Sidebar stub').closest('div.flex.h-full')
    expect(background).not.toBeNull()
    expect(background).not.toHaveAttribute('inert')
  })

  // Regression coverage for docs/settings-modal-visual-bug.md: the opaque
  // backdrop (ui/dialog.tsx) handles *visual* occlusion, but the app shell
  // behind it must also become unreachable to keyboard/assistive-tech
  // navigation while Settings is open -- `inert` is what removes it from
  // the accessibility tree and tab order on top of (not instead of) the
  // opaque backdrop.
  it('marks the background content inert once Settings is open', async () => {
    renderAppShell()
    await userEvent.click(screen.getByRole('button', { name: 'Open settings' }))

    expect(screen.getByRole('dialog')).toBeInTheDocument()
    const background = screen.getByText('Sidebar stub').closest('div.flex.h-full')
    expect(background).toHaveAttribute('inert')
  })
})
