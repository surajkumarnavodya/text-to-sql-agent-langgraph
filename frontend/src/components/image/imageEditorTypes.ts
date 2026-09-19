export type EditorTool = 'select' | 'draw' | 'eraser' | 'text' | 'rectangle' | 'ellipse' | 'mask' | 'crop'

interface BaseElement {
  id: string
  x: number
  y: number
  rotation: number
}

export interface LineElement extends BaseElement {
  kind: 'line'
  points: number[]
  stroke: string
  strokeWidth: number
  /** True for the eraser tool -- rendered with
   * globalCompositeOperation="destination-out" so it removes ink from
   * earlier strokes on the same layer instead of drawing new ink. */
  erasing: boolean
}

export interface RectElement extends BaseElement {
  kind: 'rect'
  width: number
  height: number
  stroke: string
}

export interface EllipseElement extends BaseElement {
  kind: 'ellipse'
  radiusX: number
  radiusY: number
  stroke: string
}

export interface TextElement extends BaseElement {
  kind: 'text'
  text: string
  fill: string
  fontSize: number
}

export type EditorElement = LineElement | RectElement | EllipseElement | TextElement

export interface EditorDocument {
  /** Annotation elements (draw strokes, shapes, text) -- the drawing/
   * annotation layer, undo/redo-tracked. */
  elements: EditorElement[]
  /** A separate layer of freehand strokes marking "what an AI-guided edit
   * should apply to" -- rendered translucent red, exported separately as
   * its own data URL for AiGuidedEditAdapter. Also undo/redo-tracked,
   * combined into the same history as `elements` so one Undo/Redo pair
   * covers both layers in the order they were actually drawn. */
  maskElements: LineElement[]
}

export const EMPTY_DOCUMENT: EditorDocument = { elements: [], maskElements: [] }
