import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// Two typefaces plus the mono machine voice, bundled (this app also runs
// offline inside Electron, so no CDN).
import '@fontsource-variable/instrument-sans'
import '@fontsource-variable/inter'
import '@fontsource-variable/jetbrains-mono'
import './styles/globals.css'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
