import {
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from '@tanstack/react-table'
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  ChevronLeft,
  ChevronRight,
  Download,
  Search,
  Table2,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Select } from '@/components/ui/select'
import { useSchemaTables } from '@/hooks/queries'
import { formatColumnLabel, getDisplayColumns } from '@/lib/columnFormatting'
import { downloadCsv, rowsToCsv } from '@/lib/csv'

type Row = Record<string, unknown>

/** A "fully functional datatable" (sortable columns, a global search
 * filter, and pagination) in the spirit of jQuery DataTables' feature set
 * -- built on @tanstack/react-table (headless, so it's styled to match the
 * rest of the app) rather than the jQuery library itself, which doesn't
 * integrate with React's rendering model. */
export function ResultsTable({ columns, rows }: { columns: string[]; rows: unknown[][] }) {
  const { t } = useTranslation()
  const [showTechnical, setShowTechnical] = useState(false)
  const [sorting, setSorting] = useState<SortingState>([])
  const [globalFilter, setGlobalFilter] = useState('')
  const [pagination, setPagination] = useState({ pageIndex: 0, pageSize: 10 })
  const schemaTables = useSchemaTables()

  const keyColumns = useMemo(() => {
    const set = new Set<string>()
    for (const table of schemaTables.data?.tables ?? []) {
      for (const column of table.columns) {
        if (column.is_primary_key) set.add(column.name.toLowerCase())
      }
    }
    return set
  }, [schemaTables.data])

  const { displayColumns, usedFallback } = useMemo(
    () => getDisplayColumns(columns, keyColumns),
    [columns, keyColumns],
  )
  const activeColumns = showTechnical ? columns : displayColumns
  const columnIndexes = useMemo(
    () => activeColumns.map((col) => columns.indexOf(col)),
    [activeColumns, columns],
  )

  const tableRows = useMemo<Row[]>(
    () =>
      rows.map((row) =>
        Object.fromEntries(activeColumns.map((col, i) => [col, row[columnIndexes[i]]])),
      ),
    [rows, activeColumns, columnIndexes],
  )

  const columnDefs = useMemo<ColumnDef<Row>[]>(
    () =>
      activeColumns.map((col) => ({
        accessorKey: col,
        header: showTechnical ? col : formatColumnLabel(col),
        cell: (info) => String(info.getValue() ?? ''),
      })),
    [activeColumns, showTechnical],
  )

  const table = useReactTable({
    data: tableRows,
    columns: columnDefs,
    state: { sorting, globalFilter, pagination },
    onSortingChange: setSorting,
    onGlobalFilterChange: setGlobalFilter,
    onPaginationChange: setPagination,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
  })

  const filteredRowCount = table.getFilteredRowModel().rows.length

  const handleExportCsv = () => {
    const csv = rowsToCsv(
      activeColumns,
      table.getFilteredRowModel().rows.map((row) => activeColumns.map((col) => row.original[col])),
    )
    downloadCsv(csv, 'results.csv')
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <Table2 className="h-4 w-4 text-[var(--muted-foreground)]" />
          {t('results.title')} ({filteredRowCount} {t('common.rows')})
        </h3>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-1.5 text-xs text-[var(--muted-foreground)]">
            <input
              type="checkbox"
              checked={showTechnical}
              onChange={(event) => setShowTechnical(event.target.checked)}
            />
            {t('results.showTechnical')}
          </label>
          <Button size="sm" variant="secondary" onClick={handleExportCsv}>
            <Download className="h-3.5 w-3.5" />
            {t('common.exportCsv')}
          </Button>
        </div>
      </div>
      {!showTechnical && usedFallback && (
        <p className="text-xs text-[var(--muted-foreground)]">Only identifier columns were returned.</p>
      )}

      <div className="relative w-full max-w-xs">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--muted-foreground)]" />
        <input
          value={globalFilter}
          onChange={(event) => setGlobalFilter(event.target.value)}
          placeholder={t('common.search')}
          className="h-8 w-full rounded-md border border-[var(--border)] bg-[var(--input)] pl-8 pr-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
        />
      </div>

      <div className="scroll-x max-h-[420px] rounded-md border border-[var(--border)]">
        <table className="w-full min-w-max text-left text-sm">
          <thead className="sticky top-0 bg-[var(--muted)]">
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => {
                  const sortState = header.column.getIsSorted()
                  return (
                    <th
                      key={header.id}
                      onClick={header.column.getToggleSortingHandler()}
                      className="cursor-pointer select-none whitespace-nowrap px-3 py-2 font-medium hover:bg-[var(--card)]"
                    >
                      <span className="flex items-center gap-1">
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        {sortState === 'asc' && <ArrowUp className="h-3 w-3" />}
                        {sortState === 'desc' && <ArrowDown className="h-3 w-3" />}
                        {!sortState && <ArrowUpDown className="h-3 w-3 opacity-30" />}
                      </span>
                    </th>
                  )
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.length === 0 && (
              <tr>
                <td colSpan={activeColumns.length} className="px-3 py-4 text-center text-[var(--muted-foreground)]">
                  {t('common.noResults')}
                </td>
              </tr>
            )}
            {table.getRowModel().rows.map((row) => (
              <tr key={row.id} className="border-t border-[var(--border)]">
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="whitespace-nowrap px-3 py-1.5">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {filteredRowCount > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-[var(--muted-foreground)]">
          <div className="flex items-center gap-2">
            <span>{t('common.rowsPerPage')}</span>
            <Select
              value={pagination.pageSize}
              onChange={(event) => table.setPageSize(Number(event.target.value))}
              className="h-7 py-0 text-xs"
            >
              {[10, 25, 50, 100].map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </Select>
          </div>
          <div className="flex items-center gap-2">
            <span>
              {table.getState().pagination.pageIndex + 1} {t('common.of')} {table.getPageCount()}
            </span>
            <Button
              size="icon"
              variant="ghost"
              onClick={() => table.previousPage()}
              disabled={!table.getCanPreviousPage()}
              aria-label="Previous page"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </Button>
            <Button
              size="icon"
              variant="ghost"
              onClick={() => table.nextPage()}
              disabled={!table.getCanNextPage()}
              aria-label="Next page"
            >
              <ChevronRight className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
