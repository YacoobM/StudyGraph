import { createContext, useContext, useEffect, useState } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { supabase } from '../services/supabase.js'

const AuthCtx = createContext(null)

export function AuthProvider({ children }) {
  const [session, setSession] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let mounted = true

    supabase.auth.getSession().then(({ data: { session: currentSession } }) => {
      if (mounted) {
        setSession(currentSession)
        setLoading(false)
      }
    })

    const { data: { subscription } } = supabase.auth.onAuthStateChange((_event, nextSession) => {
      setSession(nextSession)
      setLoading(false)
    })

    return () => {
      mounted = false
      subscription.unsubscribe()
    }
  }, [])

  const signIn = (email, password) => {
    return supabase.auth.signInWithPassword({ email, password })
  }

  const signUp = (email, password, name) => {
    return supabase.auth.signUp({
      email,
      password,
      options: { data: { full_name: name || '' } },
    })
  }

  const logout = () => {
    return supabase.auth.signOut()
  }

  return (
    <AuthCtx.Provider value={{ user: session?.user || null, token: session?.access_token || null, loading, signIn, signUp, logout }}>
      {children}
    </AuthCtx.Provider>
  )
}

export function useAuth() {
  return useContext(AuthCtx)
}

export function RequireAuth({ children }) {
  const { token, loading } = useAuth()
  const location = useLocation()
  if (loading) return null
  if (!token) {
    return <Navigate to="/login" state={{ from: location }} replace />
  }
  return children
}
