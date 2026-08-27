import React, { useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth.jsx'
import { login as apiLogin, register as apiRegister } from '../services/api.js'
import './Login.css'

/**
 * One screen for both signing in and creating an account.
 *
 * The mode toggle swaps only the fields that differ, and email/password keep
 * their values across the switch, so changing your mind costs nothing already
 * typed. There is no confirm-password field — a reveal toggle on the single
 * password input does the same job without typing it twice.
 */

const COPY = {
  signin: {
    heading: 'Welcome back',
    sub: 'Your graphs, documents, and quiz progress are tied to your account.',
    submit: 'Sign in',
    pending: 'Signing in…',
    autocomplete: 'current-password',
  },
  signup: {
    heading: 'Start your first graph',
    sub: 'Upload a lecture PDF and StudyGraph maps the concepts inside it.',
    submit: 'Create account',
    pending: 'Creating account…',
    autocomplete: 'new-password',
  },
}

const EMAIL_OK = (v) => /^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test(v)

export default function Login() {
  const [mode, setMode] = useState('signin')
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [reveal, setReveal] = useState(false)
  const [errors, setErrors] = useState({})
  const [formError, setFormError] = useState('')
  const [busy, setBusy] = useState(false)

  const { login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const from = location.state?.from?.pathname || '/workspace'

  const copy = COPY[mode]
  const isSignup = mode === 'signup'

  function switchMode(next) {
    setMode(next)
    setErrors({})       // errors belong to the mode that produced them
    setFormError('')
  }

  function validate() {
    const next = {}
    if (!EMAIL_OK(email.trim())) next.email = 'Enter a valid email address.'
    if (isSignup) {
      if (password.length < 8) next.password = 'Use at least 8 characters.'
    } else if (!password) {
      next.password = 'Enter your password.'
    }
    setErrors(next)
    return Object.keys(next).length === 0
  }

  async function submit(event) {
    event.preventDefault()
    setFormError('')
    if (!validate()) return

    setBusy(true)
    try {
      const data = isSignup
        ? await apiRegister(email.trim(), password, name.trim())
        : await apiLogin(email.trim(), password)

      login(data.token, data.user)
      navigate(from, { replace: true })
    } catch (err) {
      if (err.response?.status === 409) {
        // Their account exists — move them to the mode that works, keeping
        // the email so they do not retype it.
        setErrors({ email: 'That email already has an account. Sign in instead.' })
        setMode('signin')
      } else {
        setFormError(err.message || 'Something went wrong. Try again.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="sg-auth">
      <form className={`auth-card mode-${mode}`} onSubmit={submit} noValidate>
        <button type="button" className="auth-mark" onClick={() => navigate('/')}>
          StudyGraph
        </button>

        <h1>{copy.heading}</h1>
        <p className="auth-sub">{copy.sub}</p>

        <div className="seg" role="tablist" aria-label="Sign in or create an account">
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'signin'}
            className={mode === 'signin' ? 'on' : ''}
            onClick={() => switchMode('signin')}
          >
            Sign in
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'signup'}
            className={mode === 'signup' ? 'on' : ''}
            onClick={() => switchMode('signup')}
          >
            Create account
          </button>
        </div>

        <div className="auth-fields">
          {isSignup && (
            <div className="sg-field">
              <label htmlFor="authName">
                Name <span className="opt">— optional</span>
              </label>
              <input
                id="authName"
                type="text"
                autoComplete="name"
                placeholder="What should we call you?"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
          )}

          <div className="sg-field">
            <label htmlFor="authEmail">Email</label>
            <input
              id="authEmail"
              type="email"
              autoComplete="email"
              placeholder="you@university.edu"
              className={errors.email ? 'bad' : ''}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              aria-invalid={Boolean(errors.email)}
            />
            {errors.email && <div className="err show">{errors.email}</div>}
          </div>

          <div className="sg-field">
            <label htmlFor="authPassword">Password</label>
            <div className="pw-wrap">
              <input
                id="authPassword"
                type={reveal ? 'text' : 'password'}
                autoComplete={copy.autocomplete}
                placeholder={isSignup ? 'At least 8 characters' : 'Your password'}
                className={errors.password ? 'bad' : ''}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                aria-invalid={Boolean(errors.password)}
              />
              <button
                type="button"
                className="pw-toggle"
                onClick={() => setReveal((r) => !r)}
                aria-label={reveal ? 'Hide password' : 'Show password'}
              >
                {reveal ? 'Hide' : 'Show'}
              </button>
            </div>
            {isSignup && !errors.password && (
              <div className="hint">8 characters or more.</div>
            )}
            {errors.password && <div className="err show">{errors.password}</div>}
          </div>
        </div>

        <button className="auth-submit" type="submit" disabled={busy}>
          {busy ? copy.pending : copy.submit}
        </button>

        {formError && <div className="auth-formerr show">{formError}</div>}

        <p className="auth-note">
          Passwords are hashed with PBKDF2-SHA256 before they are stored.
        </p>
      </form>
    </div>
  )
}
