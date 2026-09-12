// One-off icon rasterizer for the PWA manifest -- run via `node
// scripts/generate-icons.mjs` whenever scripts/icon*.svg changes. Not part
// of the normal build (the generated PNGs are checked into public/), since
// these are static design assets, not something that needs regenerating on
// every build.
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import sharp from 'sharp'

const here = dirname(fileURLToPath(import.meta.url))
const publicDir = join(here, '..', 'public')

const icon = readFileSync(join(here, 'icon.svg'))
const iconMaskable = readFileSync(join(here, 'icon-maskable.svg'))

await Promise.all([
  sharp(icon).resize(192, 192).png().toFile(join(publicDir, 'icon-192.png')),
  sharp(icon).resize(512, 512).png().toFile(join(publicDir, 'icon-512.png')),
  sharp(iconMaskable).resize(512, 512).png().toFile(join(publicDir, 'icon-512-maskable.png')),
  sharp(icon).resize(180, 180).png().toFile(join(publicDir, 'apple-touch-icon.png')),
])

console.log('Generated icon-192.png, icon-512.png, icon-512-maskable.png, apple-touch-icon.png in public/')
