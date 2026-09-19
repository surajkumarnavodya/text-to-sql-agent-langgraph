import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { Drawer, DrawerContent, DrawerTitle, DrawerTrigger } from './drawer'

function TestDrawer() {
  return (
    <Drawer>
      <DrawerTrigger>Open menu</DrawerTrigger>
      <DrawerContent>
        <DrawerTitle>Navigation</DrawerTitle>
        <button>Item</button>
      </DrawerContent>
    </Drawer>
  )
}

describe('Drawer', () => {
  it('opens on trigger click', async () => {
    render(<TestDrawer />)
    await userEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  // Regression coverage for the same dark-mode background-bleed-through bug
  // fixed in ui/dialog.tsx (docs/settings-modal-visual-bug.md) -- the drawer
  // shares the same @radix-ui/react-dialog overlay pattern and had the same
  // `bg-black/40` (40% opaque) backdrop.
  it('renders a fully opaque backdrop, never a translucent bg-black overlay', async () => {
    render(<TestDrawer />)
    await userEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    const overlay = Array.from(document.body.querySelectorAll('*')).find((el) =>
      el.className.toString().includes('modal-backdrop'),
    )
    expect(overlay).toBeTruthy()
    expect(overlay?.className.toString()).not.toMatch(/bg-black/)
  })
})
