/**
 * Ports db/column_formatting.py's display-only transforms (human-readable
 * column labels, hiding surrogate key columns by default) so this table
 * matches the same choices the Python side makes. Pure display logic --
 * never touches the SQL that ran or the raw result data.
 *
 * Known, deliberate gap vs. the Python original: `is_probable_surrogate_key`
 * there also checks foreign-key constrained columns (via live schema
 * introspection); `/schema/tables` today only exposes each column's
 * `is_primary_key` flag, not FK info, so this port only uses the PK signal
 * plus the same name-suffix fallback. A column that's an FK but not a PK
 * anywhere may show up as "technical" here when it wouldn't on the Python
 * side -- a minor display-only discrepancy, not a data-safety one.
 */

const ABBREVIATION_EXPANSIONS: Record<string, string> = {
  cust: 'Customer',
  qty: 'Quantity',
  amt: 'Amount',
  desc: 'Description',
  num: 'Number',
}

const SURROGATE_KEY_SUFFIXES = new Set(['key', 'id'])

const CAMEL_BOUNDARY_RE = /(?<!^)(?=[A-Z][a-z])|(?<=[a-z0-9])(?=[A-Z])/g

function tokenize(rawName: string): string[] {
  const tokens: string[] = []
  for (const piece of rawName.split('_')) {
    if (!piece) continue
    tokens.push(...piece.replace(CAMEL_BOUNDARY_RE, ' ').split(/\s+/).filter(Boolean))
  }
  return tokens
}

function titleCase(word: string): string {
  return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase()
}

export function formatColumnLabel(rawName: string): string {
  const tokens = tokenize(rawName)
  if (tokens.length === 0) {
    return rawName.replaceAll('_', ' ').replace(/\w\S*/g, titleCase)
  }
  return tokens.map((token) => ABBREVIATION_EXPANSIONS[token.toLowerCase()] ?? titleCase(token)).join(' ')
}

export function isProbableSurrogateKey(columnName: string, keyColumns: Set<string>): boolean {
  if (keyColumns.has(columnName.toLowerCase())) return true
  const tokens = tokenize(columnName)
  if (tokens.length === 0) return false
  return SURROGATE_KEY_SUFFIXES.has(tokens.at(-1)!.toLowerCase())
}

export function getDisplayColumns(
  columns: string[],
  keyColumns: Set<string>,
): { displayColumns: string[]; usedFallback: boolean } {
  const filtered = columns.filter((column) => !isProbableSurrogateKey(column, keyColumns))
  if (filtered.length === 0) return { displayColumns: columns, usedFallback: true }
  return { displayColumns: filtered, usedFallback: false }
}
