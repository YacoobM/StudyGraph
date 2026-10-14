import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
// Shared styles load first so page and component styles can override them.
import './styles/shared.css'
import Landing from './pages/Landing/Landing.jsx'
import Login from './pages/Login/Login.jsx'
import NotFound from './pages/NotFound/NotFound.jsx'
import Workspace from './pages/Workspace/Workspace.jsx'
import { AuthProvider, RequireAuth } from './lib/auth.jsx'
import './styles/global.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route path="/login" element={<Login />} />
          <Route
            path="/workspace"
            element={
              <RequireAuth>
                <Workspace />
              </RequireAuth>
            }
          />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  </React.StrictMode>
)
