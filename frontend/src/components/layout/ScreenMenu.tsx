import { ChevronDown, LayoutGrid, type LucideIcon } from 'lucide-react'
import { Fragment } from 'react'
import { useTranslation } from 'react-i18next'
import { useLocation, useNavigate } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'

export interface ScreenMenuItem {
  id: string
  path: string
  label: string
  icon: LucideIcon
}

export interface ScreenMenuGroup {
  key: string
  labelKey: string
  items: ScreenMenuItem[]
}

/** The header's single dropdown for every screen besides Chat. The caller
 * passes only the groups the server allowed (`GET /navigation`), so this menu
 * never shows an entry the caller can't open -- and the menu itself isn't
 * rendered at all when there are no such entries. Items are grouped by the
 * server's own `group` field, not a list maintained here. */
export function ScreenMenu({ groups }: { groups: ScreenMenuGroup[] }) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const onMenuScreen = groups.some((group) => group.items.some((item) => item.path === pathname))

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          aria-label={t('nav.menu')}
          className={cn(
            'gap-1.5 px-2.5 text-sm font-medium sm:px-3',
            onMenuScreen && 'bg-[var(--accent-soft)] text-[var(--accent)]',
          )}
        >
          <LayoutGrid className="h-4 w-4" aria-hidden="true" />
          <span className="hidden sm:inline">{t('nav.menu')}</span>
          <ChevronDown className="hidden h-3.5 w-3.5 sm:inline" aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-[14rem]">
        {groups.map((group, index) => (
          <Fragment key={group.key}>
            {index > 0 && <DropdownMenuSeparator />}
            <DropdownMenuLabel className="text-xs uppercase tracking-wide text-[var(--muted-foreground)]">
              {t(group.labelKey)}
            </DropdownMenuLabel>
            {group.items.map((item) => {
              const Icon = item.icon
              const active = item.path === pathname
              return (
                <DropdownMenuItem
                  key={item.id}
                  aria-current={active ? 'page' : undefined}
                  className={cn('gap-2', active && 'font-medium text-[var(--accent)]')}
                  onSelect={() => navigate(item.path)}
                >
                  <Icon className="h-4 w-4" aria-hidden="true" />
                  {item.label}
                </DropdownMenuItem>
              )
            })}
          </Fragment>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
