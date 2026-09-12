import { useTranslation } from 'react-i18next'
import { Expander } from '@/components/ui/expander'
import type { SchemaTable } from '@/lib/types'

export function SchemaContextPanel({ tables }: { tables: SchemaTable[] }) {
  const { t } = useTranslation()
  if (tables.length === 0) return null

  return (
    <Expander title={`🔍 ${t('sql.schemaContext')}`}>
      <div className="flex flex-col gap-3 text-xs">
        {tables.map((table) => (
          <div key={table.table_name}>
            <p className="mb-1 font-medium">
              {table.table_name}{' '}
              <span className="text-[var(--muted-foreground)]">
                (similarity {table.similarity_score.toFixed(2)})
              </span>
            </p>
            <pre className="overflow-x-auto rounded bg-[var(--muted)] p-2 font-mono">{table.ddl}</pre>
          </div>
        ))}
      </div>
    </Expander>
  )
}
