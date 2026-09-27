import {
  AlertTriangle,
  Circle as CircleIcon,
  Crop as CropIcon,
  Download,
  Droplet,
  Eraser,
  FlipHorizontal,
  FlipVertical,
  Loader2,
  MousePointer2,
  Pencil,
  Redo2,
  RotateCcw,
  RotateCw,
  ScanText,
  Sparkles,
  Square as SquareIcon,
  Type as TypeIcon,
  Undo2,
  ZoomIn,
  ZoomOut,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Konva from 'konva'
import { Ellipse, Image as KonvaImage, Layer, Line, Rect, Stage, Text, Transformer } from 'react-konva'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { useAttachmentCapabilities } from '@/hooks/queries'
import { useHistory } from '@/hooks/useHistory'
import {
  ApiError,
  blurAttachmentRegion,
  deleteAttachment,
  extractAttachmentText,
  uploadAttachments,
} from '@/lib/api'
import {
  AiEditFailedError,
  AiEditNotConfiguredError,
  AiGuidedEditAdapter,
  type AiGuidedEditResult,
} from '@/lib/imageEditAdapter'
import type { AiImageEditOperation } from '@/lib/types'
import { cn } from '@/lib/utils'
import {
  EMPTY_DOCUMENT,
  type EditorDocument,
  type EditorElement,
  type EditorTool,
  type EllipseElement,
  type LineElement,
  type RectElement,
} from './imageEditorTypes'

const CANVAS_MAX_WIDTH = 640
const CANVAS_MAX_HEIGHT = 480
const BRUSH_COLORS = ['#ef4444', '#3b82f6', '#22c55e', '#eab308', '#111827', '#ffffff']
const ASPECT_PRESETS: { key: string; ratio: number | null }[] = [
  { key: 'image.aspectFree', ratio: null },
  { key: 'image.aspectSquare', ratio: 1 },
  { key: 'image.aspect4to3', ratio: 4 / 3 },
  { key: 'image.aspect16to9', ratio: 16 / 9 },
]

function useLoadedImage(src: string): HTMLImageElement | null {
  const [image, setImage] = useState<HTMLImageElement | null>(null)
  useEffect(() => {
    const img = new Image()
    img.onload = () => setImage(img)
    img.src = src
    return () => setImage(null)
  }, [src])
  return image
}

function randomId(prefix: string): string {
  return `${prefix}-${Math.random().toString(36).slice(2, 10)}`
}

function dataUrlToFile(dataUrl: string, filename: string): File {
  const [header, base64Data] = dataUrl.split(',')
  const mimeMatch = /data:([^;]+)/.exec(header)
  const mime = mimeMatch?.[1] ?? 'image/png'
  const binary = atob(base64Data)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
  return new File([bytes], filename, { type: mime })
}

const aiEditAdapter = new AiGuidedEditAdapter()

/** Real, generative operations only -- one quick-action preset button per
 * entry, each setting both a prefilled (editable) prompt and this fixed,
 * server-validated operation. "Blur the selected face" and "Extract the
 * selected table or chart" are deliberately NOT in this map: per this
 * feature's own quick-action routing table, blur must default to a local
 * deterministic filter (see `handleBlurFace` below) and table/chart
 * extraction is analysis, not pixel editing (see `handleExtractTable`
 * below) -- neither should ever reach the paid AI-edit endpoint. */
const AI_EDIT_PRESET_OPERATIONS: Partial<Record<string, AiImageEditOperation>> = {
  aiPresetRemoveObject: 'remove_object',
  aiPresetBlueBackground: 'region_edit',
  aiPresetReplaceSky: 'replace_sky',
  aiPresetEnhance: 'enhance',
}

type AiEditPhase = 'idle' | 'uploading' | 'processing' | 'completed' | 'failed'
type LocalActionPhase = 'idle' | 'processing' | 'completed' | 'failed'

export function ImageEditor({
  open,
  onOpenChange,
  imageSrc,
  fileName,
  onSave,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The image to edit -- the original file's object URL, or a
   * previously-saved edit's data URL if the user is re-opening an
   * already-edited attachment. */
  imageSrc: string
  fileName: string
  onSave: (dataUrl: string) => void
}) {
  const { t } = useTranslation()
  const capabilitiesQuery = useAttachmentCapabilities()
  // Off (fails closed, never open) until the capability check actually
  // returns `true` -- per this feature's own "capability flags must
  // reflect real backend readiness" requirement; a still-loading or
  // failed capability fetch must never optimistically enable a metered
  // provider call.
  const aiEditingAvailable = capabilitiesQuery.data?.image_ai_editing === true
  // `workingSrc` overrides `imageSrc` once a crop has been applied --
  // Apply Crop flattens the whole current canvas (rotation/flip/
  // annotations included) into a brand-new base image, so everything
  // after it starts from that flattened result. `imageSrc` itself (the
  // prop) is never mutated by this component.
  const [workingSrc, setWorkingSrc] = useState<string | null>(null)
  const baseImage = useLoadedImage(workingSrc ?? imageSrc)

  const [tool, setTool] = useState<EditorTool>('select')
  const [brushSize, setBrushSize] = useState(6)
  const [brushColor, setBrushColor] = useState(BRUSH_COLORS[0])
  const [rotation, setRotation] = useState(0)
  const [flipX, setFlipX] = useState(false)
  const [flipY, setFlipY] = useState(false)
  const [zoom, setZoom] = useState(1)

  // `width`/`height` below is the STAGE's own bounding box (post-rotation
  // swap, what the Stage/canvas-container are sized to); `unrotatedWidth`/
  // `unrotatedHeight` is the image's own rendered size *before* rotation
  // is applied, which is what KonvaImage's own width/height/offset need to
  // be for the standard "rotate around center" technique below: the image
  // is drawn at its unrotated size, its offset is set to half of that
  // unrotated size (its own center), and it's positioned at the stage's
  // center -- rotation and flip then pivot around that shared center
  // point instead of the node's default top-left corner, which is what
  // keeps a rotated/flipped image visually centered in the canvas instead
  // of jumping to a corner. Declared early (right after the `rotation`
  // state it depends on) since the crop-seeding effect below reads it.
  const scaledSize = useMemo(() => {
    if (!baseImage) {
      return { width: CANVAS_MAX_WIDTH, height: CANVAS_MAX_HEIGHT, unrotatedWidth: CANVAS_MAX_WIDTH, unrotatedHeight: CANVAS_MAX_HEIGHT }
    }
    const isSideways = rotation === 90 || rotation === 270
    const boundingWidth = isSideways ? baseImage.naturalHeight : baseImage.naturalWidth
    const boundingHeight = isSideways ? baseImage.naturalWidth : baseImage.naturalHeight
    const fitScale = Math.min(CANVAS_MAX_WIDTH / boundingWidth, CANVAS_MAX_HEIGHT / boundingHeight, 1)
    return {
      width: boundingWidth * fitScale,
      height: boundingHeight * fitScale,
      unrotatedWidth: baseImage.naturalWidth * fitScale,
      unrotatedHeight: baseImage.naturalHeight * fitScale,
    }
  }, [baseImage, rotation])

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [showOriginal, setShowOriginal] = useState(false)
  const [aiPrompt, setAiPrompt] = useState('')
  const [aiOperation, setAiOperation] = useState<AiImageEditOperation>('remove_object')
  const [aiPhase, setAiPhase] = useState<AiEditPhase>('idle')
  const [aiResult, setAiResult] = useState<AiGuidedEditResult | null>(null)
  const [aiError, setAiError] = useState<string | null>(null)
  const [aiMaskPreviewUrl, setAiMaskPreviewUrl] = useState<string | null>(null)
  // Deterministic, local Pillow blur (never a model call) -- see
  // `handleBlurFace` below for why this is a separate, non-AI action.
  const [blurPhase, setBlurPhase] = useState<LocalActionPhase>('idle')
  const [blurResultUrl, setBlurResultUrl] = useState<string | null>(null)
  const [blurError, setBlurError] = useState<string | null>(null)
  // OCR/vision analysis -- "Extract the selected table or chart" produces
  // structured recognized text, never a generated image.
  const [extractPhase, setExtractPhase] = useState<LocalActionPhase>('idle')
  const [extractedText, setExtractedText] = useState<string | null>(null)
  const [extractError, setExtractError] = useState<string | null>(null)
  const [cropRect, setCropRect] = useState<{ x: number; y: number; width: number; height: number } | null>(null)

  const doc = useHistory<EditorDocument>(EMPTY_DOCUMENT)
  const [inProgressLine, setInProgressLine] = useState<LineElement | null>(null)
  const [inProgressShape, setInProgressShape] = useState<RectElement | EllipseElement | null>(null)
  const drawingRef = useRef<LineElement | null>(null)
  const cropRectNodeRef = useRef<Konva.Rect>(null)
  const cropTransformerRef = useRef<Konva.Transformer>(null)
  // Tracks the id of the rect/ellipse currently being drag-sized, so
  // handleMouseMove only ever resizes the shape actually being drawn right
  // now -- not just "whatever happens to be last in the array," which
  // would keep resizing an old shape on every future mouse move once one
  // rect/ellipse existed (a real bug caught and fixed before this shipped).
  const drawingShapeIdRef = useRef<string | null>(null)
  const stageRef = useRef<Konva.Stage>(null)
  const maskLayerRef = useRef<Konva.Layer>(null)
  const selectedNodeRef = useRef<Konva.Node | null>(null)
  const transformerRef = useRef<Konva.Transformer>(null)
  // Guards every async image action (AI edit, blur, extract) against a
  // stale response overwriting the UI after the modal was closed or
  // reopened for a different image -- incremented on every open/close and
  // checked before any state-setting call in an async handler resolves.
  const requestEpochRef = useRef(0)

  // Reset all editor state back to a pristine view of `imageSrc` whenever
  // the dialog opens for a (possibly different) image -- reopening the
  // same attachment later starts a fresh session rather than replaying
  // stale in-memory edits from last time. Also bumps `requestEpochRef` --
  // closing the modal (or reopening it for a different image) while an
  // AI-edit/blur/extract request is still in flight must never let that
  // request's eventual response overwrite the UI for whatever is open now.
  useEffect(() => {
    requestEpochRef.current += 1
    if (!open) return
    doc.reset(EMPTY_DOCUMENT)
    setTool('select')
    setRotation(0)
    setFlipX(false)
    setFlipY(false)
    setZoom(1)
    setSelectedId(null)
    setShowOriginal(false)
    setAiPrompt('')
    setAiOperation('remove_object')
    setAiPhase('idle')
    setAiResult(null)
    setAiError(null)
    setAiMaskPreviewUrl(null)
    setBlurPhase('idle')
    setBlurResultUrl(null)
    setBlurError(null)
    setExtractPhase('idle')
    setExtractedText(null)
    setExtractError(null)
    setWorkingSrc(null)
    setCropRect(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- deliberately only re-runs on open/imageSrc, not on every doc identity change
  }, [open, imageSrc])

  // Seeds a centered, 80%-of-canvas default crop rect the first time the
  // crop tool is selected (not on every render while it's active, which
  // would keep resetting a rect the user is actively dragging).
  useEffect(() => {
    if (tool === 'crop' && !cropRect) {
      const width = scaledSize.width * 0.8
      const height = scaledSize.height * 0.8
      setCropRect({ x: (scaledSize.width - width) / 2, y: (scaledSize.height - height) / 2, width, height })
    }
  }, [tool, cropRect, scaledSize])

  useEffect(() => {
    if (tool === 'crop' && cropTransformerRef.current && cropRectNodeRef.current) {
      cropTransformerRef.current.nodes([cropRectNodeRef.current])
      cropTransformerRef.current.getLayer()?.batchDraw()
    }
  }, [tool, cropRect])

  useEffect(() => {
    if (tool === 'select' && selectedId && transformerRef.current && selectedNodeRef.current) {
      transformerRef.current.nodes([selectedNodeRef.current])
      transformerRef.current.getLayer()?.batchDraw()
    } else {
      transformerRef.current?.nodes([])
    }
  }, [tool, selectedId])

  const addElement = (element: EditorElement) => {
    doc.set({ ...doc.state, elements: [...doc.state.elements, element] })
  }

  const addMaskElement = (element: LineElement) => {
    doc.set({ ...doc.state, maskElements: [...doc.state.maskElements, element] })
  }

  const pointerToCanvas = (): { x: number; y: number } | null => {
    const stage = stageRef.current
    if (!stage) return null
    const pos = stage.getRelativePointerPosition()
    return pos ? { x: pos.x, y: pos.y } : null
  }

  const handleMouseDown = () => {
    if (tool === 'select') return
    const point = pointerToCanvas()
    if (!point) return

    if (tool === 'draw' || tool === 'eraser') {
      drawingRef.current = {
        id: randomId('line'),
        kind: 'line',
        x: 0,
        y: 0,
        rotation: 0,
        points: [point.x, point.y],
        stroke: brushColor,
        strokeWidth: brushSize,
        erasing: tool === 'eraser',
      }
    } else if (tool === 'mask') {
      drawingRef.current = {
        id: randomId('mask'),
        kind: 'line',
        x: 0,
        y: 0,
        rotation: 0,
        points: [point.x, point.y],
        stroke: '#ef4444',
        strokeWidth: Math.max(brushSize, 12),
        erasing: false,
      }
    } else if (tool === 'rectangle') {
      const id = randomId('rect')
      drawingShapeIdRef.current = id
      setInProgressShape({ id, kind: 'rect', x: point.x, y: point.y, rotation: 0, width: 0, height: 0, stroke: brushColor })
    } else if (tool === 'ellipse') {
      const id = randomId('ellipse')
      drawingShapeIdRef.current = id
      setInProgressShape({ id, kind: 'ellipse', x: point.x, y: point.y, rotation: 0, radiusX: 0, radiusY: 0, stroke: brushColor })
    } else if (tool === 'text') {
      const text = window.prompt(t('image.textTool'))
      if (text && text.trim()) {
        addElement({
          id: randomId('text'),
          kind: 'text',
          x: point.x,
          y: point.y,
          rotation: 0,
          text: text.trim(),
          fill: brushColor,
          fontSize: 20,
        })
      }
    }
  }

  const handleMouseMove = () => {
    const point = pointerToCanvas()
    if (!point) return

    if (drawingRef.current) {
      drawingRef.current = { ...drawingRef.current, points: [...drawingRef.current.points, point.x, point.y] }
      // Live-render the in-progress stroke by forcing the layer to redraw
      // via a shallow state touch is avoided for perf -- instead we mutate
      // the Konva node directly through a ref-free approach: simplest
      // correct option here is to commit incrementally through doc.set on
      // mouseup only, and render the *in-progress* stroke from a plain
      // React state mirror so it's visible while drawing.
      setInProgressLine(drawingRef.current)
      return
    }

    if (!drawingShapeIdRef.current || !inProgressShape) return

    // Resized only in local component state while dragging -- NOT pushed
    // into `doc` (the undo/redo-tracked history) on every mousemove, which
    // would otherwise spam one undo entry per pixel of drag instead of one
    // entry for the whole "draw this shape" gesture. Only `handleMouseUp`
    // commits it to `doc`, exactly once, matching how a freehand line
    // (`inProgressLine`) already works.
    if (inProgressShape.kind === 'rect') {
      setInProgressShape({ ...inProgressShape, width: point.x - inProgressShape.x, height: point.y - inProgressShape.y })
    } else {
      setInProgressShape({
        ...inProgressShape,
        radiusX: Math.abs(point.x - inProgressShape.x),
        radiusY: Math.abs(point.y - inProgressShape.y),
      })
    }
  }

  const handleMouseUp = () => {
    drawingShapeIdRef.current = null
    if (drawingRef.current) {
      if (drawingRef.current.points.length > 2) {
        if (tool === 'mask') addMaskElement(drawingRef.current)
        else addElement(drawingRef.current)
      }
      drawingRef.current = null
      setInProgressLine(null)
    }
    if (inProgressShape) {
      const hasRealSize =
        inProgressShape.kind === 'rect'
          ? Math.abs(inProgressShape.width) > 2 && Math.abs(inProgressShape.height) > 2
          : inProgressShape.radiusX > 2 && inProgressShape.radiusY > 2
      if (hasRealSize) addElement(inProgressShape)
      setInProgressShape(null)
    }
  }

  const applyRotate = (direction: 1 | -1) => {
    setRotation((current) => (current + direction * 90 + 360) % 360)
  }

  const flattenToDataUrl = (): string | null => {
    const stage = stageRef.current
    if (!stage) return null
    return stage.toDataURL({ pixelRatio: 2 })
  }

  const applyAspectPreset = (ratio: number | null) => {
    if (!cropRect) return
    if (ratio === null) return
    const height = cropRect.width / ratio
    const centerY = cropRect.y + cropRect.height / 2
    setCropRect({ ...cropRect, height, y: centerY - height / 2 })
  }

  /** Flattens the whole current canvas (base image + rotation/flip +
   * every annotation layer) to a bitmap, then crops that bitmap to the
   * crop rect's bounds -- everything is baked into one new base image, so
   * `elements`/`maskElements`/rotation/flip are cleared afterward rather
   * than staying around to be (incorrectly) re-applied on top of an
   * already-cropped, already-transformed result. NOTE: pixel-exactness of
   * the crop bounds (the `* zoom` scaling below, matching Konva's
   * `Stage.toDataURL({x,y,width,height})` coordinate contract) has not
   * been manually verified against a real running browser in this
   * session -- see docs/image-editing-architecture.md's "Known
   * limitations." */
  const applyCrop = () => {
    if (!cropRect || !stageRef.current) return
    const dataUrl = stageRef.current.toDataURL({
      x: cropRect.x * zoom,
      y: cropRect.y * zoom,
      width: cropRect.width * zoom,
      height: cropRect.height * zoom,
      pixelRatio: 2,
    })
    setWorkingSrc(dataUrl)
    doc.reset(EMPTY_DOCUMENT)
    setRotation(0)
    setFlipX(false)
    setFlipY(false)
    setCropRect(null)
    setTool('select')
  }

  const handleSave = () => {
    const dataUrl = flattenToDataUrl()
    if (dataUrl) onSave(dataUrl)
    onOpenChange(false)
  }

  const handleDownload = () => {
    const dataUrl = flattenToDataUrl()
    if (!dataUrl) return
    const link = document.createElement('a')
    link.href = dataUrl
    link.download = `edited-${fileName.replace(/\.[^.]+$/, '')}.png`
    link.click()
  }

  const handleResetAll = () => {
    doc.reset(EMPTY_DOCUMENT)
    setRotation(0)
    setFlipX(false)
    setFlipY(false)
    setSelectedId(null)
  }

  /** A real, isolated render of *only* the mask layer, at the exact same
   * pixel dimensions `flattenToDataUrl()` itself produces -- both come
   * from the same Stage geometry (same `pixelRatio: 2`), which is what
   * guarantees the mask and the source image line up pixel-for-pixel
   * regardless of the current zoom/rotation/crop state, without needing a
   * second offscreen Stage or any manual coordinate-transform math. Fixes
   * the "the mask sent is currently the whole flattened canvas" known
   * simplification docs/image-editing-architecture.md used to disclose.
   *
   * Every other layer is temporarily hidden and the mask layer is forced
   * to full opacity (the visible 0.45 preview opacity would otherwise
   * wash out the exported alpha values) for the duration of one
   * `toDataURL()` call, then immediately restored -- the on-screen editor
   * is never visibly affected. Returns `null` when there's nothing painted
   * (an empty mask should mean "no mask," not a real-but-blank PNG). */
  const flattenMaskOnly = (): string | null => {
    const stage = stageRef.current
    const maskLayer = maskLayerRef.current
    if (!stage || !maskLayer || doc.state.maskElements.length === 0) return null

    const otherLayers = stage.getLayers().filter((layer) => layer !== maskLayer)
    const previousVisibility = otherLayers.map((layer) => layer.visible())
    const previousMaskOpacity = maskLayer.opacity()
    otherLayers.forEach((layer) => layer.hide())
    maskLayer.opacity(1)
    stage.batchDraw()

    const dataUrl = stage.toDataURL({ pixelRatio: 2 })

    otherLayers.forEach((layer, index) => layer.visible(previousVisibility[index]))
    maskLayer.opacity(previousMaskOpacity)
    stage.batchDraw()

    return dataUrl
  }

  /** Replaces the working canvas with `dataUrl` and clears every layer on
   * top of it -- the same "bake everything into a new base image" pattern
   * `applyCrop` already established, reused here so accepting an AI-edit
   * or blur result behaves identically: the result becomes the new
   * starting point for further local edits, and nothing from before it
   * (rotation/flip/annotations/mask) is incorrectly re-applied on top. */
  const applyResultToCanvas = (dataUrl: string) => {
    setWorkingSrc(dataUrl)
    doc.reset(EMPTY_DOCUMENT)
    setRotation(0)
    setFlipX(false)
    setFlipY(false)
    setCropRect(null)
    setTool('select')
  }

  const handleAiGenerate = async () => {
    if (!aiPrompt.trim()) return
    const epoch = requestEpochRef.current
    const sourceDataUrl = flattenToDataUrl()
    if (!sourceDataUrl) return
    const maskDataUrl = flattenMaskOnly()

    setAiPhase('uploading')
    setAiError(null)
    setAiResult(null)
    setAiMaskPreviewUrl(maskDataUrl)
    try {
      setAiPhase('processing')
      const result = await aiEditAdapter.editWithInstruction({
        imageDataUrl: sourceDataUrl,
        maskDataUrl,
        instruction: aiPrompt.trim(),
        operation: aiOperation,
      })
      if (requestEpochRef.current !== epoch) return // modal closed/reopened while this was in flight
      setAiResult(result)
      setAiPhase('completed')
    } catch (error) {
      if (requestEpochRef.current !== epoch) return
      if (error instanceof AiEditNotConfiguredError) {
        setAiError(t('image.aiNotConfigured'))
      } else if (error instanceof AiEditFailedError) {
        setAiError(error.message)
      } else if (error instanceof ApiError) {
        setAiError(error.message)
      } else {
        setAiError(t('image.aiGenericError'))
      }
      setAiPhase('failed')
    }
  }

  const handleAiPresetClick = (key: string) => {
    setAiPrompt(t(`image.${key}`))
    const operation = AI_EDIT_PRESET_OPERATIONS[key]
    if (operation) setAiOperation(operation)
  }

  /** "Blur the selected face" -- capability (C)-adjacent local,
   * deterministic Pillow work (`attachments.blur.blur_region`), never the
   * paid AI-edit endpoint, per this feature's own "deterministic filter by
   * default" quick-action requirement. Requires a painted mask (the region
   * to blur); still works even when AI-guided editing itself is disabled. */
  const handleBlurFace = async () => {
    const epoch = requestEpochRef.current
    const sourceDataUrl = flattenToDataUrl()
    const maskDataUrl = flattenMaskOnly()
    if (!sourceDataUrl) return
    if (!maskDataUrl) {
      setBlurError(t('image.aiMaskRequired'))
      setBlurPhase('failed')
      return
    }

    setBlurPhase('processing')
    setBlurError(null)
    setBlurResultUrl(null)
    let sourceAttachmentId: string | null = null
    try {
      const uploadResult = await uploadAttachments([dataUrlToFile(sourceDataUrl, 'source.png')])
      if (uploadResult.attachments.length === 0) {
        throw new Error(uploadResult.errors[0]?.message ?? t('image.aiGenericError'))
      }
      sourceAttachmentId = uploadResult.attachments[0].attachment_id
      const result = await blurAttachmentRegion(sourceAttachmentId, { mask_data_url: maskDataUrl })
      if (requestEpochRef.current !== epoch) return
      setBlurResultUrl(result.image_data_url)
      setBlurPhase('completed')
    } catch (error) {
      if (requestEpochRef.current !== epoch) return
      setBlurError(error instanceof ApiError ? error.message : t('image.aiGenericError'))
      setBlurPhase('failed')
    } finally {
      if (sourceAttachmentId) void deleteAttachment(sourceAttachmentId).catch(() => {})
    }
  }

  /** "Extract the selected table or chart" -- capability (B), OCR/vision
   * *analysis*, never pixel editing: this must never produce or claim to
   * produce a generated image, per this feature's own routing requirement.
   * Runs on the whole current canvas as shown (crop first with the Crop
   * tool to isolate a specific table/chart, if the photo has more than
   * one). */
  const handleExtractTable = async () => {
    const epoch = requestEpochRef.current
    const sourceDataUrl = flattenToDataUrl()
    if (!sourceDataUrl) return

    setExtractPhase('processing')
    setExtractError(null)
    setExtractedText(null)
    let sourceAttachmentId: string | null = null
    try {
      const uploadResult = await uploadAttachments([dataUrlToFile(sourceDataUrl, 'source.png')])
      if (uploadResult.attachments.length === 0) {
        throw new Error(uploadResult.errors[0]?.message ?? t('image.aiGenericError'))
      }
      sourceAttachmentId = uploadResult.attachments[0].attachment_id
      const result = await extractAttachmentText(sourceAttachmentId)
      if (requestEpochRef.current !== epoch) return
      setExtractedText(result.raw_text)
      setExtractPhase('completed')
    } catch (error) {
      if (requestEpochRef.current !== epoch) return
      setExtractError(error instanceof ApiError ? error.message : t('image.aiGenericError'))
      setExtractPhase('failed')
    } finally {
      if (sourceAttachmentId) void deleteAttachment(sourceAttachmentId).catch(() => {})
    }
  }

  const cursorClass =
    tool === 'select' ? 'cursor-default' : tool === 'text' ? 'cursor-text' : 'cursor-crosshair'

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{t('image.editorTitle')}</DialogTitle>
        </DialogHeader>

        <div className="flex flex-wrap items-center gap-1 border-b border-[var(--border)] pb-2">
          <ToolButton icon={MousePointer2} label={t('image.selectTool')} active={tool === 'select'} onClick={() => setTool('select')} />
          <ToolButton icon={Pencil} label={t('image.draw')} active={tool === 'draw'} onClick={() => setTool('draw')} />
          <ToolButton icon={Eraser} label={t('image.eraser')} active={tool === 'eraser'} onClick={() => setTool('eraser')} />
          <ToolButton icon={TypeIcon} label={t('image.textTool')} active={tool === 'text'} onClick={() => setTool('text')} />
          <ToolButton icon={SquareIcon} label={t('image.rectangleTool')} active={tool === 'rectangle'} onClick={() => setTool('rectangle')} />
          <ToolButton icon={CircleIcon} label={t('image.ellipseTool')} active={tool === 'ellipse'} onClick={() => setTool('ellipse')} />
          <ToolButton icon={Sparkles} label={t('image.maskTool')} active={tool === 'mask'} onClick={() => setTool('mask')} />
          <ToolButton icon={CropIcon} label={t('image.crop')} active={tool === 'crop'} onClick={() => setTool('crop')} />

          <div className="mx-1 h-5 w-px bg-[var(--border)]" aria-hidden="true" />

          <Button size="icon" variant="ghost" onClick={() => applyRotate(-1)} aria-label={t('image.rotateLeft')} title={t('image.rotateLeft')}>
            <RotateCcw className="h-4 w-4" />
          </Button>
          <Button size="icon" variant="ghost" onClick={() => applyRotate(1)} aria-label={t('image.rotateRight')} title={t('image.rotateRight')}>
            <RotateCw className="h-4 w-4" />
          </Button>
          <Button size="icon" variant="ghost" onClick={() => setFlipX((v) => !v)} aria-label={t('image.flipHorizontal')} title={t('image.flipHorizontal')}>
            <FlipHorizontal className="h-4 w-4" />
          </Button>
          <Button size="icon" variant="ghost" onClick={() => setFlipY((v) => !v)} aria-label={t('image.flipVertical')} title={t('image.flipVertical')}>
            <FlipVertical className="h-4 w-4" />
          </Button>
          <Button size="icon" variant="ghost" onClick={() => setZoom((v) => Math.min(v + 0.25, 3))} aria-label={t('image.zoomIn')} title={t('image.zoomIn')}>
            <ZoomIn className="h-4 w-4" />
          </Button>
          <Button size="icon" variant="ghost" onClick={() => setZoom((v) => Math.max(v - 0.25, 0.5))} aria-label={t('image.zoomOut')} title={t('image.zoomOut')}>
            <ZoomOut className="h-4 w-4" />
          </Button>

          <div className="mx-1 h-5 w-px bg-[var(--border)]" aria-hidden="true" />

          <Button size="icon" variant="ghost" onClick={doc.undo} disabled={!doc.canUndo} aria-label={t('image.undo')} title={t('image.undo')}>
            <Undo2 className="h-4 w-4" />
          </Button>
          <Button size="icon" variant="ghost" onClick={doc.redo} disabled={!doc.canRedo} aria-label={t('image.redo')} title={t('image.redo')}>
            <Redo2 className="h-4 w-4" />
          </Button>
        </div>

        {(tool === 'draw' || tool === 'eraser' || tool === 'mask') && (
          <div className="flex items-center gap-3 py-1 text-xs">
            <label className="flex items-center gap-1.5">
              {t('image.brushSize')}
              <input
                type="range"
                min={2}
                max={40}
                value={brushSize}
                onChange={(event) => setBrushSize(Number(event.target.value))}
                aria-label={t('image.brushSize')}
              />
            </label>
            {tool !== 'mask' && (
              <div className="flex items-center gap-1" role="group" aria-label={t('image.brushColor')}>
                {BRUSH_COLORS.map((color) => (
                  <button
                    key={color}
                    type="button"
                    onClick={() => setBrushColor(color)}
                    aria-label={color}
                    aria-pressed={brushColor === color}
                    className={cn(
                      'h-5 w-5 rounded-full border',
                      brushColor === color ? 'ring-2 ring-[var(--focus-ring)]' : 'border-[var(--border)]',
                    )}
                    style={{ backgroundColor: color }}
                  />
                ))}
              </div>
            )}
          </div>
        )}

        {tool === 'crop' && (
          <div className="flex items-center gap-3 py-1 text-xs">
            <div className="flex items-center gap-1" role="group" aria-label={t('image.crop')}>
              {ASPECT_PRESETS.map((preset) => (
                <button
                  key={preset.key}
                  type="button"
                  onClick={() => applyAspectPreset(preset.ratio)}
                  className="rounded-full border border-[var(--border)] px-2.5 py-1 hover:bg-[var(--muted)]"
                >
                  {t(preset.key)}
                </button>
              ))}
            </div>
            <Button size="sm" variant="primary" onClick={applyCrop} disabled={!cropRect}>
              <CropIcon className="h-3.5 w-3.5" />
              {t('image.applyCrop')}
            </Button>
          </div>
        )}

        <div className="flex items-center justify-between py-1">
          <div className="flex items-center gap-2 text-xs">
            <label className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={showOriginal}
                onChange={(event) => setShowOriginal(event.target.checked)}
              />
              {t('image.beforeAfter')}
            </label>
          </div>
          <Button size="sm" variant="ghost" onClick={handleResetAll}>
            {t('image.resetEdits')}
          </Button>
        </div>

        <div className="flex justify-center overflow-auto rounded-md border border-[var(--border)] bg-[var(--muted)] p-2">
          <Stage
            ref={stageRef}
            width={scaledSize.width * zoom}
            height={scaledSize.height * zoom}
            scale={{ x: zoom, y: zoom }}
            onMouseDown={handleMouseDown}
            onMouseMove={handleMouseMove}
            onMouseUp={handleMouseUp}
            onTouchStart={handleMouseDown}
            onTouchMove={handleMouseMove}
            onTouchEnd={handleMouseUp}
            className={cursorClass}
          >
            <Layer listening={false}>
              {baseImage && (
                <KonvaImage
                  image={baseImage}
                  width={scaledSize.unrotatedWidth}
                  height={scaledSize.unrotatedHeight}
                  offsetX={scaledSize.unrotatedWidth / 2}
                  offsetY={scaledSize.unrotatedHeight / 2}
                  x={scaledSize.width / 2}
                  y={scaledSize.height / 2}
                  rotation={rotation}
                  scaleX={flipX ? -1 : 1}
                  scaleY={flipY ? -1 : 1}
                />
              )}
            </Layer>

            {!showOriginal && (
              <Layer>
                {doc.state.elements.map((element) => (
                  <EditorElementNode
                    key={element.id}
                    element={element}
                    selectable={tool === 'select'}
                    isSelected={selectedId === element.id}
                    onSelect={(node) => {
                      setSelectedId(element.id)
                      selectedNodeRef.current = node
                    }}
                    onChange={(next) => {
                      doc.set({
                        ...doc.state,
                        elements: doc.state.elements.map((existing) => (existing.id === next.id ? next : existing)),
                      })
                    }}
                  />
                ))}
                {inProgressLine && (
                  <Line
                    points={inProgressLine.points}
                    stroke={inProgressLine.stroke}
                    strokeWidth={inProgressLine.strokeWidth}
                    lineCap="round"
                    lineJoin="round"
                    globalCompositeOperation={inProgressLine.erasing ? 'destination-out' : 'source-over'}
                  />
                )}
                {inProgressShape?.kind === 'rect' && (
                  <Rect
                    x={inProgressShape.x}
                    y={inProgressShape.y}
                    width={inProgressShape.width}
                    height={inProgressShape.height}
                    stroke={inProgressShape.stroke}
                    strokeWidth={2}
                    dash={[4, 4]}
                  />
                )}
                {inProgressShape?.kind === 'ellipse' && (
                  <Ellipse
                    x={inProgressShape.x}
                    y={inProgressShape.y}
                    radiusX={inProgressShape.radiusX}
                    radiusY={inProgressShape.radiusY}
                    stroke={inProgressShape.stroke}
                    strokeWidth={2}
                    dash={[4, 4]}
                  />
                )}
                {tool === 'select' && <Transformer ref={transformerRef} rotateEnabled resizeEnabled />}
              </Layer>
            )}

            {!showOriginal && doc.state.maskElements.length > 0 && (
              <Layer ref={maskLayerRef} opacity={0.45} listening={false}>
                {doc.state.maskElements.map((element) => (
                  <Line
                    key={element.id}
                    points={element.points}
                    stroke={element.stroke}
                    strokeWidth={element.strokeWidth}
                    lineCap="round"
                    lineJoin="round"
                  />
                ))}
                {inProgressLine && tool === 'mask' && (
                  <Line points={inProgressLine.points} stroke={inProgressLine.stroke} strokeWidth={inProgressLine.strokeWidth} lineCap="round" lineJoin="round" />
                )}
              </Layer>
            )}

            {tool === 'crop' && cropRect && (
              <Layer>
                <Rect
                  ref={cropRectNodeRef}
                  x={cropRect.x}
                  y={cropRect.y}
                  width={cropRect.width}
                  height={cropRect.height}
                  stroke="var(--accent)"
                  strokeWidth={2}
                  dash={[6, 4]}
                  fill="rgba(0,0,0,0.15)"
                  draggable
                  dragBoundFunc={(pos) => ({
                    x: Math.min(Math.max(pos.x, 0), scaledSize.width - cropRect.width),
                    y: Math.min(Math.max(pos.y, 0), scaledSize.height - cropRect.height),
                  })}
                  onDragEnd={(event) => setCropRect({ ...cropRect, x: event.target.x(), y: event.target.y() })}
                  onTransformEnd={(event) => {
                    const node = event.target
                    const scaleX = node.scaleX()
                    const scaleY = node.scaleY()
                    node.scaleX(1)
                    node.scaleY(1)
                    setCropRect({
                      x: node.x(),
                      y: node.y(),
                      width: Math.max(20, cropRect.width * scaleX),
                      height: Math.max(20, cropRect.height * scaleY),
                    })
                  }}
                />
                <Transformer
                  ref={cropTransformerRef}
                  rotateEnabled={false}
                  boundBoxFunc={(oldBox, newBox) =>
                    newBox.width < 20 || newBox.height < 20 ? oldBox : newBox
                  }
                />
              </Layer>
            )}
          </Stage>
        </div>

        {tool === 'mask' && <p className="text-xs text-[var(--muted-foreground)]">{t('image.maskHint')}</p>}

        <div className="mt-3 rounded-md border border-[var(--border)] p-3">
          <p className="mb-1.5 flex items-center gap-1.5 text-sm font-medium">
            <Sparkles className="h-3.5 w-3.5" /> {t('image.aiSectionTitle')}
          </p>

          {!capabilitiesQuery.isLoading && !aiEditingAvailable && (
            <p className="mb-2 flex items-start gap-1.5 rounded-md border border-[var(--border)] bg-[var(--muted)] p-2 text-xs text-[var(--muted-foreground)]">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              {t('image.aiNotConfigured')}
            </p>
          )}

          <div className="flex flex-wrap gap-1.5">
            {(['aiPresetRemoveObject', 'aiPresetBlueBackground', 'aiPresetReplaceSky', 'aiPresetEnhance'] as const).map(
              (key) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => handleAiPresetClick(key)}
                  disabled={!aiEditingAvailable}
                  className="rounded-full border border-[var(--border)] px-2.5 py-1 text-xs hover:bg-[var(--muted)] disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {t(`image.${key}`)}
                </button>
              ),
            )}
          </div>
          <div className="mt-2 flex gap-2">
            <input
              value={aiPrompt}
              onChange={(event) => setAiPrompt(event.target.value)}
              placeholder={t('image.aiPromptPlaceholder')}
              aria-label={t('image.aiPromptLabel')}
              disabled={!aiEditingAvailable}
              className="h-9 flex-1 rounded-md border border-[var(--border)] bg-[var(--input)] px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--focus-ring)] disabled:opacity-50"
            />
            <Button
              variant="secondary"
              onClick={() => void handleAiGenerate()}
              disabled={
                !aiEditingAvailable ||
                !aiPrompt.trim() ||
                aiPhase === 'uploading' ||
                aiPhase === 'processing'
              }
            >
              {aiPhase === 'uploading' || aiPhase === 'processing' ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                t('image.aiGenerate')
              )}
            </Button>
          </div>

          {(aiPhase === 'uploading' || aiPhase === 'processing') && (
            <p className="mt-2 flex items-center gap-1.5 text-xs text-[var(--muted-foreground)]">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              {aiPhase === 'uploading' ? t('image.aiPhaseUploading') : t('image.aiPhaseProcessing')}
            </p>
          )}

          {aiMaskPreviewUrl && (aiPhase === 'processing' || aiPhase === 'completed' || aiPhase === 'failed') && (
            <div className="mt-2 flex items-center gap-2">
              <img
                src={aiMaskPreviewUrl}
                alt={t('image.aiMaskPreview')}
                className="h-12 w-12 rounded border border-[var(--border)] bg-[var(--muted)] object-contain"
              />
              <span className="text-xs text-[var(--muted-foreground)]">{t('image.aiMaskPreview')}</span>
            </div>
          )}

          {aiPhase === 'failed' && aiError && (
            <p className="mt-2 flex items-start gap-1.5 rounded-md border border-[var(--danger)]/30 bg-[var(--danger)]/10 p-2 text-xs text-[var(--danger)]">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              {aiError}
            </p>
          )}

          {aiPhase === 'completed' && aiResult && (
            <div className="mt-2 flex flex-col gap-2 rounded-md border border-[var(--border)] p-2">
              <img
                src={aiResult.imageDataUrl}
                alt={t('image.aiResultTitle')}
                className="mx-auto max-h-56 rounded border border-[var(--border)] object-contain"
              />
              {aiResult.warnings.map((warning) => (
                <p key={warning} className="flex items-start gap-1.5 text-xs text-[var(--warning)]">
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  {warning}
                </p>
              ))}
              <div className="flex justify-center gap-2">
                <Button size="sm" variant="ghost" onClick={() => setAiPhase('idle')}>
                  {t('image.aiDiscardResult')}
                </Button>
                <Button size="sm" variant="primary" onClick={() => applyResultToCanvas(aiResult.imageDataUrl)}>
                  {t('image.aiApplyResult')}
                </Button>
              </div>
            </div>
          )}
        </div>

        <div className="mt-3 flex flex-col gap-3 rounded-md border border-[var(--border)] p-3 sm:flex-row">
          <div className="flex-1">
            <p className="mb-1.5 flex items-center gap-1.5 text-sm font-medium">
              <Droplet className="h-3.5 w-3.5" /> {t('image.aiPresetBlurFace')}
            </p>
            <p className="mb-2 text-xs text-[var(--muted-foreground)]">{t('image.blurHint')}</p>
            <Button size="sm" variant="secondary" onClick={() => void handleBlurFace()} disabled={blurPhase === 'processing'}>
              {blurPhase === 'processing' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Droplet className="h-4 w-4" />}
              {t('image.aiPresetBlurFace')}
            </Button>
            {blurPhase === 'failed' && blurError && (
              <p className="mt-2 flex items-start gap-1.5 rounded-md border border-[var(--danger)]/30 bg-[var(--danger)]/10 p-2 text-xs text-[var(--danger)]">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                {blurError}
              </p>
            )}
            {blurPhase === 'completed' && blurResultUrl && (
              <div className="mt-2 flex flex-col gap-2 rounded-md border border-[var(--border)] p-2">
                <img
                  src={blurResultUrl}
                  alt={t('image.blurResultTitle')}
                  className="mx-auto max-h-40 rounded border border-[var(--border)] object-contain"
                />
                <div className="flex justify-center gap-2">
                  <Button size="sm" variant="ghost" onClick={() => setBlurPhase('idle')}>
                    {t('image.aiDiscardResult')}
                  </Button>
                  <Button size="sm" variant="primary" onClick={() => applyResultToCanvas(blurResultUrl)}>
                    {t('image.aiApplyResult')}
                  </Button>
                </div>
              </div>
            )}
          </div>

          <div className="flex-1">
            <p className="mb-1.5 flex items-center gap-1.5 text-sm font-medium">
              <ScanText className="h-3.5 w-3.5" /> {t('image.aiPresetExtractTable')}
            </p>
            <p className="mb-2 text-xs text-[var(--muted-foreground)]">{t('image.extractHint')}</p>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => void handleExtractTable()}
              disabled={extractPhase === 'processing'}
            >
              {extractPhase === 'processing' ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <ScanText className="h-4 w-4" />
              )}
              {t('image.aiPresetExtractTable')}
            </Button>
            {extractPhase === 'failed' && extractError && (
              <p className="mt-2 flex items-start gap-1.5 rounded-md border border-[var(--danger)]/30 bg-[var(--danger)]/10 p-2 text-xs text-[var(--danger)]">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                {extractError}
              </p>
            )}
            {extractPhase === 'completed' && (
              <div className="mt-2 rounded-md border border-[var(--border)] p-2">
                <p className="mb-1 text-xs font-medium text-[var(--muted-foreground)]">
                  {t('image.extractedTextTitle')}
                </p>
                <pre className="max-h-32 overflow-auto whitespace-pre-wrap break-words text-xs">
                  {extractedText && extractedText.trim() ? extractedText : t('image.extractedTextEmpty')}
                </pre>
              </div>
            )}
          </div>
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={handleDownload}>
            <Download className="h-4 w-4" />
            {t('image.download')}
          </Button>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            {t('image.cancelEdits')}
          </Button>
          <Button variant="primary" onClick={handleSave}>
            {t('image.confirmEdits')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ToolButton({
  icon: Icon,
  label,
  active,
  onClick,
}: {
  icon: typeof MousePointer2
  label: string
  active: boolean
  onClick: () => void
}) {
  return (
    <Button
      size="icon"
      variant={active ? 'primary' : 'ghost'}
      onClick={onClick}
      aria-label={label}
      aria-pressed={active}
      title={label}
    >
      <Icon className="h-4 w-4" />
    </Button>
  )
}

function EditorElementNode({
  element,
  selectable,
  isSelected,
  onSelect,
  onChange,
}: {
  element: EditorElement
  selectable: boolean
  isSelected: boolean
  onSelect: (node: Konva.Node) => void
  onChange: (element: EditorElement) => void
}) {
  const commonProps = {
    draggable: selectable,
    onClick: (event: Konva.KonvaEventObject<MouseEvent>) => selectable && onSelect(event.target),
    onTap: (event: Konva.KonvaEventObject<Event>) => selectable && onSelect(event.target),
    onDragEnd: (event: Konva.KonvaEventObject<DragEvent>) =>
      onChange({ ...element, x: event.target.x(), y: event.target.y() }),
    onTransformEnd: (event: Konva.KonvaEventObject<Event>) => {
      const node = event.target
      onChange({ ...element, x: node.x(), y: node.y(), rotation: node.rotation() })
    },
    // Selection is indicated by the Transformer's own resize handles
    // (attached to this node when isSelected -- see the effect above that
    // sets transformerRef.nodes()), not by recoloring the shape's own
    // stroke -- doing both would fight over the single `stroke` prop each
    // shape element below already sets from its own data.
    shadowColor: isSelected ? 'var(--accent)' : undefined,
    shadowBlur: isSelected ? 8 : 0,
    shadowOpacity: isSelected ? 0.6 : 0,
  }

  if (element.kind === 'line') {
    return (
      <Line
        points={element.points}
        stroke={element.stroke}
        strokeWidth={element.strokeWidth}
        lineCap="round"
        lineJoin="round"
        globalCompositeOperation={element.erasing ? 'destination-out' : 'source-over'}
        x={element.x}
        y={element.y}
        rotation={element.rotation}
        draggable={selectable}
        onClick={(event) => selectable && onSelect(event.target)}
        onDragEnd={(event) => onChange({ ...element, x: event.target.x(), y: event.target.y() })}
      />
    )
  }
  if (element.kind === 'rect') {
    return <Rect x={element.x} y={element.y} width={element.width} height={element.height} stroke={element.stroke} strokeWidth={2} rotation={element.rotation} {...commonProps} />
  }
  if (element.kind === 'ellipse') {
    return <Ellipse x={element.x} y={element.y} radiusX={element.radiusX} radiusY={element.radiusY} stroke={element.stroke} strokeWidth={2} rotation={element.rotation} {...commonProps} />
  }
  return <Text x={element.x} y={element.y} text={element.text} fill={element.fill} fontSize={element.fontSize} rotation={element.rotation} {...commonProps} />
}
