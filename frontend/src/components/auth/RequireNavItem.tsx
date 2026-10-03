import { useEffect, useRef, type ReactNode } from 'react'
import { useLocation, Link } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { useNavigation } from '@/hooks/queries'
import { reportAccessDenied } from '@/lib/navigationApi'
import type { NavigationOut } from '@/lib/types'

/** Route guard for one screen (Prompt 32).
 *
 * The screen renders only when the **server** lists it in this caller's
 * navigation. Hiding a tab is not access control, so a user who types the URL
 * gets the forbidden page instead. The backend would refuse the page's own
 * data calls regardless. The guard only keeps the screen from pretending to
 * work.
 *
 * Loading and error are explicit states, never a blank screen. An allowed
 * caller sees the screen as soon as the navigation response arrives. */
export function RequireNavItem({ screen, children }: { screen: string; children: ReactNode }) {
  const navigation = useNavigation()
  const location = useLocation()
  const reported = useRef(false)

  const allowed = navigation.data ? navigation.data.items.some((item) => item.id === screen) : false
  const denied = navigation.data !== undefined && !allowed

  useEffect(() => {
    // Audit each refusal once per visit, not on every re-render.
    if (denied && !reported.current) {
      reported.current = true
      void reportAccessDenied(location.pathname)
    }
  }, [denied, location.pathname])

  if (navigation.isPending) {
    return (
      <p role="status" className="p-6 text-sm text-[var(--muted-foreground)]">
        Checking your access…
      </p>
    )
  }

  if (navigation.isError) {
    return (
      <div role="alert" className="flex flex-col items-start gap-3 p-6">
        <p className="text-sm text-[var(--danger)]">We couldn&apos;t check your access to this screen.</p>
        <Button size="sm" variant="secondary" onClick={() => void navigation.refetch()}>
          Try again
        </Button>
      </div>
    )
  }

  if (!allowed) {
    return <ForbiddenPage navigation={navigation.data} />
  }

  return <>{children}</>
}

/** Shown in place of a screen the caller may not use. It names the problem
 * plainly, points at the screens they can use, and moves focus to its heading so
 * the change is announced to assistive technology. */
export function ForbiddenPage({ navigation }: { navigation: NavigationOut | undefined }) {
  const headingRef = useRef<HTMLHeadingElement>(null)

  useEffect(() => {
    headingRef.current?.focus()
  }, [])

  // The first screen this caller *can* open, or none. Never a screen they
  // cannot use, so the link itself cannot dead-end.
  const home = navigation?.items[0] ?? null

  return (
    <section aria-labelledby="forbidden-title" className="mx-auto flex max-w-xl flex-col gap-3 p-6">
      <h1 id="forbidden-title" ref={headingRef} tabIndex={-1} className="text-lg font-semibold outline-none">
        You don&apos;t have access to this screen
      </h1>
      <p className="text-sm text-[var(--muted-foreground)]">
        Your account&apos;s role doesn&apos;t include this area. If you need it, ask an administrator to
        review your access.
      </p>
      {home && (
        <Link
          to={home.path}
          className="w-fit text-sm font-medium text-[var(--accent)] underline underline-offset-2"
        >
          Go to a screen you can use
        </Link>
      )}
    </section>
  )
}
