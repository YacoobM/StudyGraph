import { createClient } from '@supabase/supabase-js'

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL
const supabasePublishableKey = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY

// Keep each browser tab's Supabase session independent. Supabase otherwise
// uses shared localStorage and auth broadcasts, so signing in as another user
// in a second tab replaces the first tab's session too.
const tabStorageKey = 'studygraph.tab.id'
let tabId = sessionStorage.getItem(tabStorageKey)
if (!tabId) {
  tabId = `${Date.now()}-${Math.random().toString(36).slice(2)}`
  sessionStorage.setItem(tabStorageKey, tabId)
}

if (!supabaseUrl || !supabasePublishableKey) {
  throw new Error('Missing VITE_SUPABASE_URL or VITE_SUPABASE_PUBLISHABLE_KEY.')
}

export const supabase = createClient(supabaseUrl, supabasePublishableKey, {
  auth: {
    storage: sessionStorage,
    storageKey: `studygraph.auth.${tabId}`,
    persistSession: true,
    autoRefreshToken: true,
    detectSessionInUrl: true,
  },
})
