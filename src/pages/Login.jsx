import React, { useState } from 'react'
import { useNavigate, Link, useLocation } from 'react-router-dom'
import { useAuth } from '../auth.jsx'
import { login as apiLogin, register as apiRegister } from '../services/api'
import './Login.css'

export default function Login() {
  const [mode, setMode] = useState('signin') // 'signin' | 'signup'
  const [email, setEmail] = useState('demo@graph.com')
  const [password, setPassword] = useState('demo123')
  const [name, setName] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const { login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const from = location.state?.from?.pathname || '/builder'

  const submit = async (e) => {
    e.preventDefault()
    setErr('')
    setBusy(true)
    try {
      const data = mode === 'signin'
        ? await apiLogin(email, password)
        : await apiRegister(email, password, name)
      login(data.token, data.user)
      navigate(from, { replace: true })
    } catch (e) {
      setErr(e?.response?.data?.error || 'Something went wrong')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-bg">
        <svg viewBox="0 0 800 800" preserveAspectRatio="xMidYMid slice">
          <defs>
            <radialGradient id="g1" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stopColor="#5dd4c4" stopOpacity="0.4" />
              <stop offset="100%" stopColor="#5dd4c4" stopOpacity="0" />
            </radialGradient>
          </defs>
          <circle cx="200" cy="200" r="280" fill="url(#g1)" />
          <circle cx="600" cy="600" r="240" fill="url(#g1)" opacity="0.6" />
        </svg>
      </div>

      <Link to="/" className="back-home">← Back home</Link>

      <div className="login-card">
        <div className="login-brand">
          <svg width="36" height="36" viewBox="0 0 64 64">
            <circle cx="32" cy="18" r="9" fill="#5dd4c4"/>
            <circle cx="14" cy="46" r="7" fill="#f9a8d4"/>
            <circle cx="50" cy="46" r="7" fill="#a3e635"/>
            <line x1="32" y1="27" x2="14" y2="40" stroke="#5b6473" strokeWidth="2"/>
            <line x1="32" y1="27" x2="50" y2="40" stroke="#5b6473" strokeWidth="2"/>
            <line x1="21" y1="46" x2="43" y2="46" stroke="#5b6473" strokeWidth="2"/>
          </svg>
          <span>StudyGraph</span>
        </div>

        <h1 className="login-title">
          {mode === 'signin' ? 'Welcome back' : 'Create your account'}
        </h1>
        <p className="login-sub">
          {mode === 'signin'
            ? 'Sign in to access your knowledge graphs.'
            : 'Start building your personal knowledge graph today.'}
        </p>

        <form onSubmit={submit} className="login-form">
          {mode === 'signup' && (
            <div className="field">
              <label>Name</label>
              <input
                className="input"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Your name"
              />
            </div>
          )}
          <div className="field">
            <label>Email</label>
            <input
              className="input"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@university.edu"
              required
            />
          </div>
          <div className="field">
            <label>Password</label>
            <input
              className="input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              required
            />
          </div>

          {err && <div className="form-err">{err}</div>}

          <button className="btn btn-primary btn-lg login-submit" disabled={busy}>
            {busy ? 'Please wait…' : (mode === 'signin' ? 'Sign in' : 'Create account')}
          </button>

          <div className="login-switch">
            {mode === 'signin' ? (
              <>
                New here?{' '}
                <button type="button" onClick={() => setMode('signup')}>Create an account</button>
              </>
            ) : (
              <>
                Already have an account?{' '}
                <button type="button" onClick={() => setMode('signin')}>Sign in</button>
              </>
            )}
          </div>
        </form>

        <div className="demo-hint">
          <span className="dot dot-success" />
          Demo:&nbsp;<code>demo@graph.com</code>&nbsp;/&nbsp;<code>demo123</code>
        </div>
      </div>
    </div>
  )
}
