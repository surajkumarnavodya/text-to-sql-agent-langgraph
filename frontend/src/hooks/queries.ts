import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { deleteDocument, getHealth, getSchemaTables, listDocuments, refreshSchema, uploadDocument } from '@/lib/api'
import type { Collection, SensitivityCategory } from '@/lib/types'

export function useHealth() {
  return useQuery({ queryKey: ['health'], queryFn: getHealth, staleTime: 30_000 })
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
