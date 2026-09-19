import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { CopyButton } from './copy-button'

describe('CopyButton', () => {
  beforeEach(() => {
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } })
  })

  it('copies the text returned by getText when clicked', async () => {
    render(<CopyButton getText={() => 'SELECT * FROM orders'} />)
    await userEvent.click(screen.getByRole('button'))
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('SELECT * FROM orders')
  })

  it('shows a temporary "Copied!" confirmation', async () => {
    render(<CopyButton getText={() => 'SELECT 1'} />)
    await userEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(await screen.findByRole('button', { name: 'Copied!' })).toBeInTheDocument()
  })
})
