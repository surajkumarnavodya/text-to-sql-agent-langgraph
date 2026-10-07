import { describe, expect, it } from 'vitest'
import {
  buildSpeechChunks,
  chunkSpeechBlocks,
  DEFAULT_SPEECH_OPTIONS,
  markdownToSpeechBlocks,
} from './readAloudText'

const readSqlOptions = { ...DEFAULT_SPEECH_OPTIONS, sqlMode: 'read' as const }

describe('markdownToSpeechBlocks -- prose', () => {
  it('speaks headings and list items naturally, without Markdown syntax', () => {
    const blocks = markdownToSpeechBlocks('## Revenue Summary\n\n- India: 40%\n- Germany: 20%')
    expect(blocks).toEqual(['Revenue Summary.', 'India: 40 percent.', 'Germany: 20 percent.'])
  })

  it('strips emphasis markers but keeps the words', () => {
    expect(markdownToSpeechBlocks('**Revenue** is *up* 5%.')).toEqual(['Revenue is up 5 percent.'])
  })

  it('leaves snake_case identifiers intact', () => {
    expect(markdownToSpeechBlocks('The customer_id column is unique.')).toEqual([
      'The customer_id column is unique.',
    ])
  })

  it('keeps link text and drops the URL', () => {
    expect(markdownToSpeechBlocks('See [the report](https://example.com/r?id=7) now')).toEqual([
      'See the report now.',
    ])
  })

  it('drops bare URLs entirely', () => {
    expect(markdownToSpeechBlocks('Visit https://example.com/a?b=1 today')).toEqual(['Visit today.'])
  })

  it('strips HTML tags rather than speaking them', () => {
    const [block] = markdownToSpeechBlocks('<script>alert(1)</script>Hello')
    expect(block).not.toContain('<')
    expect(block).toContain('Hello')
  })

  it('speaks ordered lists with their numbers', () => {
    expect(markdownToSpeechBlocks('1. First\n2. Second')).toEqual(['1. First.', '2. Second.'])
  })

  it('ignores horizontal rules', () => {
    expect(markdownToSpeechBlocks('A\n\n---\n\nB')).toEqual(['A.', 'B.'])
  })

  it('renders buildAnswerMarkdown\'s insight label as plain speech', () => {
    expect(markdownToSpeechBlocks('**Insight**: Revenue grew.')).toEqual(['Insight: Revenue grew.'])
  })
})

describe('markdownToSpeechBlocks -- sources', () => {
  it('replaces the sources list with one natural sentence and never reads its URLs', () => {
    const markdown = 'Answer text.\n\n**Sources:**\n- https://a.example.com/x?utm=1\n- report.pdf'
    expect(markdownToSpeechBlocks(markdown)).toEqual(['Answer text.', 'Sources are available for this answer.'])
  })
})

describe('markdownToSpeechBlocks -- SQL and code', () => {
  const sql = '```sql\nSELECT country, COUNT(*)\nFROM orders\nGROUP BY country\n```'

  it('announces SQL by default and never reads it', () => {
    expect(markdownToSpeechBlocks(sql)).toEqual(['A SQL query is shown on screen.'])
  })

  it('reads short SQL with a lead-in when sqlMode is "read"', () => {
    expect(markdownToSpeechBlocks(sql, readSqlOptions)).toEqual([
      'SQL query follows. SELECT country, COUNT(*) FROM orders GROUP BY country.',
    ])
  })

  it('caps long SQL in "read" mode and says the rest is on screen', () => {
    const longSql = `SELECT ${Array.from({ length: 200 }, (_, i) => `col_${i}`).join(', ')} FROM t`
    const [block] = markdownToSpeechBlocks(`\`\`\`sql\n${longSql}\n\`\`\``, readSqlOptions)
    expect(block.startsWith('SQL query follows.')).toBe(true)
    expect(block).toContain('The rest of the query is shown on screen.')
    expect(block.length).toBeLessThan(longSql.length)
  })

  it('announces non-SQL code blocks without reading them', () => {
    expect(markdownToSpeechBlocks('```python\nprint(1)\n```', readSqlOptions)).toEqual([
      'A code block is shown on screen.',
    ])
  })
})

describe('markdownToSpeechBlocks -- tables', () => {
  const twoColumn = '| Country | Revenue |\n|---|---|\n| India | 100 |\n| USA | 80 |'

  it('describes a two-column table and reads each row as "label: value"', () => {
    expect(markdownToSpeechBlocks(twoColumn)).toEqual([
      'Table with two columns: Country and Revenue.',
      'India: 100.',
      'USA: 80.',
    ])
  })

  it('names every column for a wider table', () => {
    const markdown = '| A | B | C |\n|---|---|---|\n| 1 | 2 | 3 |'
    expect(markdownToSpeechBlocks(markdown)).toEqual([
      'Table with three columns: A, B, and C.',
      'A: 1, B: 2, C: 3.',
    ])
  })

  it('summarizes rows beyond the limit instead of reading them all', () => {
    const rows = Array.from({ length: 25 }, (_, i) => `| r${i} | ${i} |`).join('\n')
    const blocks = markdownToSpeechBlocks(`| K | V |\n|---|---|\n${rows}`)
    expect(blocks).toHaveLength(1 + DEFAULT_SPEECH_OPTIONS.maxTableRows + 1)
    expect(blocks.at(-1)).toBe('5 more rows are shown on screen.')
  })

  it('says so when a table has no rows', () => {
    expect(markdownToSpeechBlocks('| A | B |\n|---|---|')).toEqual([
      'Table with two columns: A and B.',
      'The table has no rows.',
    ])
  })

  it('converts percentages inside table cells', () => {
    expect(markdownToSpeechBlocks('| Region | Share |\n|---|---|\n| EU | 40% |')).toContain('EU: 40 percent.')
  })
})

describe('buildSpeechChunks', () => {
  it('returns no chunks for an empty or whitespace-only answer', () => {
    expect(buildSpeechChunks('')).toEqual([])
    expect(buildSpeechChunks('   \n\n  ')).toEqual([])
  })

  it('keeps reading order and every chunk within the limit for a long answer', () => {
    const sentences = Array.from({ length: 30 }, (_, i) => `This is sentence number ${i + 1} about revenue.`)
    const chunks = buildSpeechChunks(sentences.join(' '))

    expect(chunks.length).toBeGreaterThan(1)
    for (const chunk of chunks) expect(chunk.length).toBeLessThanOrEqual(DEFAULT_SPEECH_OPTIONS.maxChunkChars)
    expect(chunks[0].startsWith('This is sentence number 1 ')).toBe(true)
    expect(chunks.join(' ')).toBe(sentences.join(' '))
  })

  it('never splits a word, even inside one long sentence', () => {
    const words = Array.from({ length: 80 }, (_, i) => `word${i}`)
    const chunks = chunkSpeechBlocks([words.join(' ')], 60)

    expect(chunks.length).toBeGreaterThan(1)
    for (const chunk of chunks) expect(chunk.length).toBeLessThanOrEqual(60)
    expect(chunks.join(' ').split(' ')).toEqual(words)
  })

  it('keeps a single token longer than the limit whole rather than cutting it mid-word', () => {
    const token = 'x'.repeat(300)
    expect(chunkSpeechBlocks([token], 200)).toEqual([token])
  })

  it('does not create tiny chunks: short sentences pack together up to the limit', () => {
    const chunks = chunkSpeechBlocks(['One. Two. Three. Four. Five.'], 200)
    expect(chunks).toEqual(['One. Two. Three. Four. Five.'])
  })
})
