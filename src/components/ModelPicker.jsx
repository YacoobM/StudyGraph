import React, { useEffect, useState } from 'react'
import { listModels } from '../services/api'
import './ModelPicker.css'

/**
 * Two-step picker: choose provider, then choose model.
 * Reads /api/models on mount to know what's available.
 *
 * Calls onChange({ provider, model }) whenever the user picks something.
 */
export default function ModelPicker({ value, onChange }) {
  const [catalogue, setCatalogue] = useState([])
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let mounted = true
    listModels().then((data) => {
      if (!mounted) return
      setCatalogue(data.providers || [])
      setLoading(false)
      // Initialize default if none set
      if (!value?.provider && data.default) {
        onChange?.(data.default)
      }
    }).catch(() => setLoading(false))
    return () => { mounted = false }
  }, [])

  const selectedProvider = catalogue.find(p => p.id === value?.provider)
  const selectedModel = selectedProvider?.models.find(m => m.id === value?.model)

  const handlePick = (providerId, modelId) => {
    onChange?.({ provider: providerId, model: modelId })
    setOpen(false)
  }

  const labelText = loading
    ? 'Loading models…'
    : (selectedProvider && selectedModel)
      ? `${selectedProvider.name} · ${selectedModel.label}`
      : 'Choose a model'

  // Close menu when clicking outside
  useEffect(() => {
    if (!open) return
    const onDoc = (e) => {
      if (!e.target.closest('.mp-wrap')) setOpen(false)
    }
    document.addEventListener('click', onDoc)
    return () => document.removeEventListener('click', onDoc)
  }, [open])

  return (
    <div className="mp-wrap">
      <button className="mp-trigger" onClick={(e) => { e.stopPropagation(); setOpen(o => !o) }}>
        <span className="mp-trigger-text">{labelText}</span>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
             style={{ transform: open ? 'rotate(180deg)' : 'rotate(0)', transition: 'transform 150ms' }}>
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>

      {open && (
        <div className="mp-menu">
          <div className="mp-menu-head">Select extraction model</div>
          {catalogue.length === 0 && (
            <div className="mp-empty">No providers configured. Set keys in backend/.env</div>
          )}
          {catalogue.map(prov => (
            <div key={prov.id} className="mp-provider">
              <div className="mp-provider-head">
                <span className={`mp-status ${prov.available ? 'mp-on' : 'mp-off'}`} />
                <span className="mp-provider-name">{prov.name}</span>
                {!prov.available && prov.env_var && (
                  <span className="mp-hint">set {prov.env_var}</span>
                )}
              </div>
              {prov.models.map(m => {
                const isActive = value?.provider === prov.id && value?.model === m.id
                return (
                  <button
                    key={m.id}
                    className={`mp-model ${isActive ? 'active' : ''} ${!prov.available ? 'disabled' : ''}`}
                    disabled={!prov.available}
                    onClick={() => handlePick(prov.id, m.id)}
                  >
                    <span className="mp-model-label">{m.label}</span>
                    <span className="mp-model-id">{m.id}</span>
                    {isActive && <span className="mp-check">✓</span>}
                  </button>
                )
              })}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
