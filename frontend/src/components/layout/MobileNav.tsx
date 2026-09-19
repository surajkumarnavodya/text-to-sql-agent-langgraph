import { Menu } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Sidebar } from '@/components/layout/Sidebar'
import { Button } from '@/components/ui/button'
import { Drawer, DrawerContent, DrawerTitle, DrawerTrigger } from '@/components/ui/drawer'

/** Hamburger trigger + slide-in drawer, visible only below the `lg`
 * breakpoint where the persistent Sidebar (AppShell) is hidden. Renders
 * the exact same `Sidebar` component the desktop uses -- no separate
 * "mobile history list" implementation to keep in sync -- just wrapped in
 * a Drawer and passed `onNavigate` to close itself once a conversation is
 * picked. */
export function MobileNav() {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)

  return (
    <Drawer open={open} onOpenChange={setOpen}>
      <DrawerTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="lg:hidden"
          aria-label={t('header.openNavigation')}
          title={t('header.openNavigation')}
        >
          <Menu className="h-4 w-4" />
        </Button>
      </DrawerTrigger>
      <DrawerContent side="left" aria-describedby={undefined}>
        <DrawerTitle className="sr-only">{t('history.title')}</DrawerTitle>
        <Sidebar onNavigate={() => setOpen(false)} />
      </DrawerContent>
    </Drawer>
  )
}
