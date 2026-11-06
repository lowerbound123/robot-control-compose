export function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url; link.download = filename
  document.body.append(link)
  try { link.click() } finally {
    link.remove()
    // Allow browsers to consume the blob before releasing our reference.
    setTimeout(() => URL.revokeObjectURL(url), 60_000)
  }
}
export const fileTime = () => new Date().toISOString().replace(/[:.]/g, '-')
