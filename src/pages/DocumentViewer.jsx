import { useEffect, useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import PdfViewerModal from '../components/PdfViewerModal'
import '../styles/studygraph.css'
import { getDocumentFileUrl } from '../services/api'

export default function DocumentViewer() {
  const { filename } = useParams()
  const [searchParams] = useSearchParams()
  const [fileUrl, setFileUrl] = useState('')

  const page = parseInt(searchParams.get('page') || '1', 10)
  const highlightText = searchParams.get('highlight') || ''

  // Build the file URL when filename changes
  useEffect(() => {
    if (filename) {
      setFileUrl(getDocumentFileUrl(filename))
    }
  }, [filename])

  if (!filename || !fileUrl) {
    return (
      <div className="doc-viewer loading">
        <div className="doc-viewer-spinner" />
        <p>Loading document…</p>
      </div>
    )
  }

  const handleClose = () => {
    // In a new tab, we can't navigate back, so just close the tab
    if (window.opener) {
      window.close()
    } else {
      window.history.back()
    }
  }

  return (
    <div className="doc-viewer-page">
      <PdfViewerModal
        fileUrl={fileUrl}
        fileName={filename}
        page={page}
        highlightText={highlightText || undefined}
        onClose={handleClose}
      />
    </div>
  )
}