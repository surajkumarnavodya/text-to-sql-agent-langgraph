import { sql } from '@codemirror/lang-sql'
import CodeMirror from '@uiw/react-codemirror'
import { resolveThemeMode } from '@/lib/theme'
import { useSettingsStore } from '@/store/settingsStore'

export function SqlEditor({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const themeMode = useSettingsStore((state) => state.themeMode)
  const resolved = resolveThemeMode(themeMode)

  return (
    <CodeMirror
      value={value}
      onChange={onChange}
      theme={resolved}
      extensions={[sql()]}
      basicSetup={{ lineNumbers: true, foldGutter: false }}
      className="overflow-hidden rounded-md border border-[var(--border)] text-sm"
      style={{ fontFamily: 'var(--font-mono)' }}
      minHeight="80px"
    />
  )
}
