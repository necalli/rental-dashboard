export const getSessionId = () => {
  if (typeof window === 'undefined') return ''
  const storageKey = 'sentiment-session-id'
  let sessionId = window.localStorage.getItem(storageKey)
  if (!sessionId) {
    sessionId = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`
    window.localStorage.setItem(storageKey, sessionId)
  }
  return sessionId
}
