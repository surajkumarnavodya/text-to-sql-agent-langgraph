// Excel/Sheets/LibreOffice all interpret a cell whose *first* character is
// one of these as the start of a formula, even when the CSV field itself is
// syntactically just a quoted string -- a query result column containing
// e.g. `=HYPERLINK("http://evil","click")` (real, attacker-plantable DB
// content, not just a CSV-syntax concern) would execute as a formula the
// moment the downloaded file is opened in a spreadsheet app. Prefixing with
// a single quote is the standard mitigation (OWASP CSV Injection guidance):
// spreadsheet apps treat a leading `'` as "force text" and hide it from the
// displayed cell, so this neutralizes the formula trigger without changing
// what the user sees.
const _FORMULA_TRIGGER_RE = /^[=+\-@\t\r]/

function escapeCsvCell(value: unknown): string {
  let text = String(value ?? '')
  if (_FORMULA_TRIGGER_RE.test(text)) {
    text = `'${text}`
  }
  if (/[",\n]/.test(text)) {
    return `"${text.replaceAll('"', '""')}"`
  }
  return text
}

export function rowsToCsv(columns: string[], rows: unknown[][]): string {
  const lines = [columns.map(escapeCsvCell).join(',')]
  for (const row of rows) {
    lines.push(row.map(escapeCsvCell).join(','))
  }
  return lines.join('\r\n')
}

function downloadBlob(content: string, filename: string, mimeType: string): void {
  const blob = new Blob([content], { type: mimeType })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}

export function downloadCsv(csv: string, filename: string): void {
  downloadBlob(csv, filename, 'text/csv;charset=utf-8;')
}
