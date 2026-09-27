import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  deleteDocument,
  getAttachmentCapabilities,
  getAvailableModels,
  getHealth,
  getSchemaTables,
  listDocuments,
  refreshSchema,
  uploadDocument,
} from '@/lib/api'
import type { Collection, SensitivityCategory } from '@/lib/types'

export function useHealth() {
  return useQuery({ queryKey: ['health'], queryFn: getHealth, staleTime: 30_000 })
}

/** What this deployment can actually do with an attachment right now
 * (vision/OCR/resize/text-removal) -- see AttachmentCapabilities's own
 * docstring. Cached the same 30s as useHealth above, since it changes only
 * with server config, never per-request. */
export function useAttachmentCapabilities() {
  return useQuery({
    queryKey: ['attachment-capabilities'],
    queryFn: getAttachmentCapabilities,
    staleTime: 30_000,
  })
}

/** The Ollama Text-to-SQL model registry (GET /models) -- which models are
 * configured/allowed and which are actually installed locally right now.
 * Cached the same 30s as useHealth above: model availability changes only
 * with server config/local `ollama pull` state, never per-request, and the
 * backend must not be re-queried on every question. */
export function useAvailableModels() {
  return useQuery({
    queryKey: ['models'],
    queryFn: getAvailableModels,
    staleTime: 30_000,
  })
}

export function useSchemaTables() {
  return useQuery({ queryKey: ['schema-tables'], queryFn: () => getSchemaTables() })
}

export function useRefreshSchema() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: refreshSchema,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['schema-tables'] })
    },
  })
}

export function useDocuments(collection?: Collection) {
  return useQuery({
    queryKey: ['documents', collection ?? 'all'],
    queryFn: () => listDocuments(collection),
  })
}

export function useUploadDocument(collection: Collection) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ file, sensitivityCategory }: { file: File; sensitivityCategory?: SensitivityCategory }) =>
      uploadDocument(file, collection, sensitivityCategory ?? undefined),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['documents'] })
    },
  })
}

export function useDeleteDocument() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: deleteDocument,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['documents'] })
    },
  })
}
