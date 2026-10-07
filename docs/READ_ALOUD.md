# Read aloud

Each answered question has a **Read aloud** button. It speaks the answer
through the browser's built-in speech synthesis. The feature is client-side
only. There is no server endpoint, no audio file, and no stored audio.

## What gets spoken

The button speaks the same plain-text export that **Copy** and **Download** use
(`buildAnswerMarkdown`). It does not read the screen, so buttons, labels,
tooltips and CSS are never spoken.

The speech text is built by `frontend/src/lib/readAloudText.ts`:

- **Tables** are read row by row, up to 20 body rows (tunable with
  `maxTableRows`). A table with more rows than that adds one trailing sentence
  ("N more rows are shown on screen") instead of reading the rest.
- **SQL** is announced as a summary by default. The `sqlMode: 'read'` option
  reads it, capped at 400 characters. The answer itself is never changed. SQL
  is never run from this path.
- **Long text** is split into chunks of about 200 characters (`maxChunkChars`),
  at sentence boundaries where possible, so each utterance stays short enough to
  start quickly and to pause cleanly.
- **Percentages and bare URLs** are spoken as words, and links keep only their
  visible text.

## Playback behaviour

The playback controller is an explicit state machine
(`frontend/src/lib/readAloud.ts`):

```
idle --start--> reading <--pause/resume--> paused
reading|paused --stop--> stopped --start--> reading
reading --last chunk ends--> idle
reading --engine error--> error --start (Retry)--> reading
```

- Only one answer speaks at a time. Starting another answer cancels the current
  one first.
- Every utterance callback is tagged with the session that created it. A
  cancelled or superseded session cannot change the state of the session that
  replaced it.
- Leaving the page (or unmounting the answer) stops speech only if that answer
  is the one speaking.
- The speech rate is fixed at `1` in this version.

The button is never hidden. When the browser has no speech synthesis
(`isSupported === false`), it stays visible but marked `aria-disabled`, with
its title and a toast explaining that read aloud is not supported — never a
silently broken control.

## Privacy

- **The application sends nothing for speech.** The text is produced and spoken
  in the browser, with no request to this server or any other.
- **The browser's voice can be a network service.** In some browsers, some
  voices are cloud-based. The browser then sends the text to that voice service.
  This is the browser's behaviour, not the app's. Users who need strictly local
  speech should pick an installed local voice in their browser or operating
  system settings.
- No speech text is written to logs, storage or the analytics store.

## Accessibility and i18n

The button's label and state text are translated in all five locales
(`en`, `es`, `fr`, `hi`, `mr`). The status is announced through the button's
own label, so screen readers hear the change.

## Testing

Covered by unit tests in `frontend/src/lib/readAloud.test.ts`,
`frontend/src/lib/readAloudText.test.ts`,
`frontend/src/components/chat/ReadAloudButton.test.tsx` and
`frontend/src/components/chat/TurnCard.actions.test.tsx`. The tests use a fake
speech engine, so they run without a browser voice.

## Known limitations

- Speech quality, voice choice and language coverage depend on the browser and
  the operating system. The app does not ship a voice.
- Playback is not available on the shared-view page or in the server-side
  conversation export. Those surfaces are outside this feature.
- Only the answer text is read. A chart, its legend and the table's column
  headers are not described beyond what the plain-text export contains.
- The rate is fixed at `1`. There is no per-user speed control yet.
