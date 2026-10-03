import { type FormEvent, type ReactNode, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Select } from '@/components/ui/select'
import { useCreateOnboardingJob } from '@/hooks/queries'
import { ApiError } from '@/lib/api'
import type { OnboardingJob } from '@/lib/types'

/** Matches `db.connection.SUPPORTED_DB_TYPES` exactly (db/connection.py) --
 * a small, stable display list, not a duplicate of that module's own
 * dialect/driver-resolution logic, which stays entirely server-side. */
const PROVIDERS: { value: string; label: string; defaultPort: number }[] = [
  { value: 'mssql', label: 'SQL Server', defaultPort: 1433 },
  { value: 'postgresql', label: 'PostgreSQL', defaultPort: 5432 },
  { value: 'mysql', label: 'MySQL', defaultPort: 3306 },
  { value: 'oracle', label: 'Oracle', defaultPort: 1521 },
]

/** Step 1-3 of the onboarding wizard: pick a provider, enter connection
 * details, and submit. There is no separate "test connection" action
 * from "create" -- `POST /onboarding/jobs` tests the connection
 * (`db.connection.test_connection`) and only creates the job if it
 * succeeds, in one atomic call (`api/onboarding.py::create_onboarding_job`'s
 * own docstring). `db_password` is held only in this form's local state
 * and in the one request body sent -- never logged, never stored in any
 * client-side cache (React Query's own cache only ever holds this
 * component's *response*, an `OnboardingJob`, which has no password
 * field at all).
 *
 * A job's tenant is resolved automatically, server-side, from the
 * caller's own authenticated account (`security.tenancy
 * .resolve_actor_tenant_id`) -- never a value this form collects or
 * sends. There is deliberately no "select tenant" control here: this
 * app's tenant model never accepts a tenant from a client request (see
 * `docs/MULTI_TENANCY.md` rule 1), and building a selector for a value
 * the server would ignore would be exactly the kind of mock/decorative
 * control the UI must not present as real. */
export function CreateJobForm({ onCreated }: { onCreated: (job: OnboardingJob) => void }) {
  const createJob = useCreateOnboardingJob()
  const [label, setLabel] = useState('')
  const [dbType, setDbType] = useState('mssql')
  const [host, setHost] = useState('')
  const [port, setPort] = useState(String(PROVIDERS[0].defaultPort))
  const [dbName, setDbName] = useState('')
  const [user, setUser] = useState('')
  const [password, setPassword] = useState('')
  const [schema, setSchema] = useState('')
  const [error, setError] = useState<string | null>(null)

  const handleProviderChange = (value: string) => {
    setDbType(value)
    const provider = PROVIDERS.find((p) => p.value === value)
    if (provider) setPort(String(provider.defaultPort))
  }

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    try {
      const job = await createJob.mutateAsync({
        database_label: label,
        db_type: dbType,
        db_host: host || null,
        db_port: port ? Number(port) : null,
        db_name: dbName || null,
        db_user: user || null,
        db_password: password || null,
        db_schema: schema || null,
      })
      setPassword('') // never keep the secret in this form's state after a successful submit
      onCreated(job)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not create the onboarding job.')
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4 rounded-lg border border-[var(--border)] bg-[var(--card)] p-4">
      <h2 className="text-sm font-semibold">New onboarding job</h2>

      <Field label="Database label" htmlFor="ob-label">
        <input
          id="ob-label"
          required
          value={label}
          onChange={(event) => setLabel(event.target.value)}
          placeholder="e.g. Acme Corp Production Warehouse"
          className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
        />
      </Field>

      <Field label="Database provider" htmlFor="ob-provider">
        <Select
          id="ob-provider"
          value={dbType}
          onChange={(event) => handleProviderChange(event.target.value)}
        >
          {PROVIDERS.map((provider) => (
            <option key={provider.value} value={provider.value}>
              {provider.label}
            </option>
          ))}
        </Select>
      </Field>

      <div className="grid gap-3 sm:grid-cols-[2fr_1fr]">
        <Field label="Host" htmlFor="ob-host">
          <input
            id="ob-host"
            value={host}
            onChange={(event) => setHost(event.target.value)}
            placeholder="db.internal.example.com"
            className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
          />
        </Field>
        <Field label="Port" htmlFor="ob-port">
          <input
            id="ob-port"
            type="number"
            value={port}
            onChange={(event) => setPort(event.target.value)}
            className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
          />
        </Field>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Database name" htmlFor="ob-dbname">
          <input
            id="ob-dbname"
            value={dbName}
            onChange={(event) => setDbName(event.target.value)}
            className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
          />
        </Field>
        <Field label="Schema (optional)" htmlFor="ob-schema">
          <input
            id="ob-schema"
            value={schema}
            onChange={(event) => setSchema(event.target.value)}
            placeholder="defaults to your user's own schema"
            className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
          />
        </Field>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Username" htmlFor="ob-user">
          <input
            id="ob-user"
            value={user}
            onChange={(event) => setUser(event.target.value)}
            className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
          />
        </Field>
        <Field label="Password" htmlFor="ob-password">
          <input
            id="ob-password"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
          />
        </Field>
      </div>
      <p className="text-xs text-[var(--muted-foreground)]">
        This password is used once to test the connection and is never stored -- it is not saved
        anywhere, including by this app, and will need to be re-entered for discovery and publish.
      </p>

      {error && <p className="text-sm text-[var(--danger)]">{error}</p>}

      <Button type="submit" disabled={createJob.isPending || !label || !dbType}>
        {createJob.isPending ? 'Testing connection…' : 'Test connection & create job'}
      </Button>
    </form>
  )
}

function Field({ label, htmlFor, children }: { label: string; htmlFor: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={htmlFor} className="text-xs font-medium text-[var(--muted-foreground)]">
        {label}
      </label>
      {children}
    </div>
  )
}
