import { describe, expect, it } from 'vitest'
import { rowsToCsv } from './csv'

describe('rowsToCsv -- CSV formula-injection protection (OWASP CSV Injection)', () => {
  it.each([
    ['=HYPERLINK("http://evil.example","click")', "'=HYPERLINK"],
    ['+1+1', "'+1+1"],
    ['-1+1', "'-1+1"],
    ['@SUM(1,1)', "'@SUM"],
  ])('prefixes a leading formula-trigger character %s with a quote', (value, expectedPrefix) => {
    // Some of these also contain a comma, which additionally triggers the
    // (independent) CSV-quoting rule -- so the assertion is "the escaped
    // prefix appears in the field" rather than "the field starts with it,"
    // covered separately (and unambiguously) by the dedicated
    // comma-plus-formula-prefix test further below.
    const csv = rowsToCsv(['col'], [[value]])
    const dataLine = csv.split('\r\n')[1]
    expect(dataLine).toContain(expectedPrefix)
    // The raw dangerous character must never be the literal first byte a
    // spreadsheet application reads for this field.
    expect(dataLine.startsWith(value[0])).toBe(false)
  })

  it('does the same for a column header, not just row values', () => {
    const csv = rowsToCsv(['=cmd|/c calc'], [['ordinary value']])
    const headerLine = csv.split('\r\n')[0]
    expect(headerLine.startsWith("'=cmd")).toBe(true)
  })

  it('leaves an ordinary value starting with a letter or digit untouched', () => {
    const csv = rowsToCsv(['col'], [['Revenue was 42000']])
    expect(csv).toContain('Revenue was 42000')
    expect(csv).not.toContain("'Revenue")
  })

  it('still quotes a value containing a comma or newline, independent of formula-escaping', () => {
    const csv = rowsToCsv(['col'], [['a,b']])
    expect(csv).toContain('"a,b"')
  })

  it('escapes an embedded double quote by doubling it', () => {
    const csv = rowsToCsv(['col'], [['say "hi"']])
    expect(csv).toContain('"say ""hi"""')
  })

  it('combines formula-prefix and quoting when both apply', () => {
    const csv = rowsToCsv(['col'], [['=A1,B1']])
    const dataLine = csv.split('\r\n')[1]
    // Formula-trigger prefixed first, then the whole (now comma-containing) value is quoted.
    expect(dataLine).toBe('"\'=A1,B1"')
  })

  it('treats null/undefined cells as an empty string, never "null"/"undefined"', () => {
    const csv = rowsToCsv(['col'], [[null], [undefined]])
    const lines = csv.split('\r\n')
    expect(lines[1]).toBe('')
    expect(lines[2]).toBe('')
  })
})
