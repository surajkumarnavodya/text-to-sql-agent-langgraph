import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { useSettingsStore } from '@/store/settingsStore'
import { SidebarToggle } from './SidebarToggle'

const initialSettingsState = useSettingsStore.getState()

describe('SidebarToggle', () => {
  afterEach(() => {
    useSettingsStore.setState(initialSettingsState, true)
  })

  it('reflects the expanded state: aria-expanded=true, labeled "Collapse sidebar"', () => {
    useSettingsStore.setState({ sidebarCollapsed: false })
    render(<SidebarToggle controls="app-sidebar" />)
    const button = screen.getByRole('button', { name: /collapse sidebar/i })
    expect(button).toHaveAttribute('aria-expanded', 'true')
    expect(button).toHaveAttribute('aria-controls', 'app-sidebar')
  })

  it('reflects the collapsed state: aria-expanded=false, labeled "Expand sidebar"', () => {
    useSettingsStore.setState({ sidebarCollapsed: true })
    render(<SidebarToggle controls="app-sidebar" />)
    expect(screen.getByRole('button', { name: /expand sidebar/i })).toHaveAttribute('aria-expanded', 'false')
  })

  it('toggles the single shared sidebarCollapsed store value on click', async () => {
    useSettingsStore.setState({ sidebarCollapsed: false })
    render(<SidebarToggle controls="app-sidebar" />)

    await userEvent.click(screen.getByRole('button', { name: /collapse sidebar/i }))
    expect(useSettingsStore.getState().sidebarCollapsed).toBe(true)

    await userEvent.click(screen.getByRole('button', { name: /expand sidebar/i }))
    expect(useSettingsStore.getState().sidebarCollapsed).toBe(false)
  })
})
