import React, { useEffect, useRef, useState, useCallback } from 'react'
import * as pdfjsLib from 'pdfjs-dist'
import pdfjsWorker from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import './PdfViewerModal.css'

pdfjsLib.GlobalWorkerOptions.workerSrc = pdfjsWorker

/*
 * PdfViewerModal
 * Opens a source PDF at a given page and, when highlightText is provided,
 * searches that page's text content for it and draws highlight boxes over
 * the matches. Search runs entirely client-side against pdf.js's own text
 * layer (not the render canvas) — it's a case-insensitive substring match
 * on the page's extracted text, so hyphenation/spacing differences from how
 * the concept name was extracted can occasionally miss; when nothing
 * matches, the page still opens, just without a highlight.
 */
export default function PdfViewerModal({ fileUrl, fileName, page = 1, highlightText, onClose }) {
  const canvasRef = useRef(null)
  const containerRef = useRef(null)
  const renderTokenRef = useRef(0)

  const [pdfDoc, setPdfDoc] = useState(null)
  const [numPages, setNumPages] = useState(0)
  const [currentPage, setCurrentPage] = useState(page || 1)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [highlightRects, setHighlightRects] = useState([])
  const [matchCount, setMatchCount] = useState(null)

  // Load the document itself whenever the target file changes.
  useEffect(() => {
    let cancelled = false
    setPdfDoc(null)
    setError(null)
    setLoading(true)
    setCurrentPage(page || 1)

    pdfjsLib.getDocument(fileUrl).promise
      .then((doc) => {
        if (cancelled) return
        setPdfDoc(doc)
        setNumPages(doc.numPages)
      })
      .catch((err) => {
        if (!cancelled) setError(err?.message || 'Failed to load PDF')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileUrl])

  // Reset to the cited page whenever a different source is opened.
  useEffect(() => { setCurrentPage(page || 1) }, [page])

  const renderPage = useCallback(async () => {
    if (!pdfDoc || !canvasRef.current) return
    const myToken = ++renderTokenRef.current
    setLoading(true)
    setError(null)
    try {
      const pdfPage = await pdfDoc.getPage(currentPage)
      const containerWidth = containerRef.current?.clientWidth || 800
      const containerHeight = containerRef.current?.clientHeight || 600
      const baseViewport = pdfPage.getViewport({ scale: 1 })
      const scale = Math.max(0.4, Math.min(3, Math.min(
        (containerWidth - 40) / baseViewport.width,
        (containerHeight - 40) / baseViewport.height,
      )))
      const viewport = pdfPage.getViewport({ scale })

      const canvas = canvasRef.current
      const context = canvas.getContext('2d')
      canvas.width = viewport.width
      canvas.height = viewport.height

      await pdfPage.render({ canvasContext: context, viewport }).promise
      if (renderTokenRef.current !== myToken) return // a newer render started; drop this one

      if (highlightText) {
        const textContent = await pdfPage.getTextContent()
        if (renderTokenRef.current !== myToken) return
        const rects = findTextRects(textContent, viewport, highlightText)
        setHighlightRects(rects)
        setMatchCount(rects.length)
      } else {
        setHighlightRects([])
        setMatchCount(null)
      }
    } catch (err) {
      if (renderTokenRef.current === myToken) setError(err?.message || 'Failed to render page')
    } finally {
      if (renderTokenRef.current === myToken) setLoading(false)
    }
  }, [pdfDoc, currentPage, highlightText])

  useEffect(() => { renderPage() }, [renderPage])

  const goPrev = () => setCurrentPage((p) => Math.max(1, p - 1))
  const goNext = () => setCurrentPage((p) => (numPages ? Math.min(numPages, p + 1) : p + 1))

  return (
    <div className="pdf-modal-overlay" onClick={(e) => { e.stopPropagation(); onClose() }}>
      <div className="pdf-modal" onClick={(e) => e.stopPropagation()}>
        <div className="pdf-modal-header">
          <span className="pdf-modal-title" title={fileName}>{fileName}</span>
          <div className="pdf-modal-nav">
            <button className="pdf-nav-btn" onClick={goPrev} disabled={currentPage <= 1}>‹</button>
            <span className="pdf-page-indicator">Page {currentPage}{numPages ? ` of ${numPages}` : ''}</span>
            <button className="pdf-nav-btn" onClick={goNext} disabled={numPages ? currentPage >= numPages : false}>›</button>
          </div>
          <button className="pdf-modal-close" onClick={onClose}>×</button>
        </div>

        <div className="pdf-modal-body" ref={containerRef}>
          {error ? (
            <div className="pdf-modal-message pdf-modal-error">
              Couldn't load this PDF ({error}). It may have been uploaded before source files started being saved.
            </div>
          ) : (
            <div className="pdf-canvas-wrap">
              <canvas ref={canvasRef} />
              {highlightRects.map((r, i) => (
                <div
                  key={i}
                  className="pdf-highlight"
                  style={{ left: r.left, top: r.top, width: r.width, height: r.height }}
                />
              ))}
            </div>
          )}
          {loading && !error && <div className="pdf-modal-message pdf-modal-loading">Loading…</div>}
          {!loading && !error && highlightText && matchCount === 0 && (
            <div className="pdf-no-match-note">"{highlightText}" wasn't found in the text on this page.</div>
          )}
        </div>
      </div>
    </div>
  )
}

// Finds every occurrence's item indexes of `searchText` inside a page's text
// content, then converts each matched item's PDF-space transform into a
// viewport-pixel rect using pdf.js's own transform utility — the same
// technique pdf.js's built-in text layer uses to position selectable text.
function findTextRects(textContent, viewport, searchText) {
  const needle = searchText.trim().toLowerCase()
  if (!needle) return []

  let combined = ''
  const charMap = []
  textContent.items.forEach((item, itemIndex) => {
    const str = item.str || ''
    for (let c = 0; c < str.length; c++) charMap.push(itemIndex)
    combined += str.toLowerCase()
    // A boundary between items that isn't a real character avoids two
    // adjacent items accidentally fusing into a false substring match.
    combined += ' '
    charMap.push(-1)
  })

  const matchedItemIndexes = new Set()
  let searchFrom = 0
  while (true) {
    const matchIndex = combined.indexOf(needle, searchFrom)
    if (matchIndex === -1) break
    for (let i = matchIndex; i < matchIndex + needle.length; i++) {
      const idx = charMap[i]
      if (idx !== -1) matchedItemIndexes.add(idx)
    }
    searchFrom = matchIndex + needle.length
  }

  return Array.from(matchedItemIndexes).map((itemIndex) => {
    const item = textContent.items[itemIndex]
    const tx = pdfjsLib.Util.transform(viewport.transform, item.transform)
    const scaleX = Math.hypot(tx[0], tx[1])
    const fontHeight = Math.hypot(tx[2], tx[3])
    return {
      left: tx[4],
      top: tx[5] - fontHeight,
      width: item.width * scaleX,
      height: fontHeight * 1.2,
    }
  })
}
