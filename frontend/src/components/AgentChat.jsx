import { useCallback, useEffect, useRef, useState } from 'react'
import Linkify from 'linkify-react'
import { MessageSquare, Send, Loader2, FileText, Paperclip } from 'lucide-react'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from '@/components/ui/sheet.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Textarea } from '@/components/ui/textarea.jsx'
import { ScrollArea } from '@/components/ui/scroll-area.jsx'

const API_BASE =
  import.meta.env.VITE_AGENT_API_BASE_URL ||
  import.meta.env.VITE_API_BASE_URL ||
  'http://localhost:5002'
const RAG_USER_ID = import.meta.env.VITE_RAG_USER_ID || 'rental-dashboard'

const initialMessage = {
  role: 'assistant',
  content:
    'Hi! I can help with rental workflow analysis, listing/comparison context, and pipeline health insights.'
}

const SOURCE_LIMIT = 8
const SUGGESTED_PROMPTS = [
  { label: 'Pipeline health', value: 'show me pipeline health metrics' },
  { label: 'Show jobs', value: 'show jobs' },
  { label: 'Search runs', value: 'show search runs' },
  { label: 'Ingested listings', value: 'show ingested listings' },
  { label: 'Job status', value: 'job status <job_uuid>' },
  { label: 'Listing summary', value: 'listing summary <listing_id>' }
]

const parseSseFrames = (buffer) => {
  const frames = []
  let remainder = String(buffer || '').replace(/\r\n/g, '\n')
  while (true) {
    const markerIndex = remainder.indexOf('\n\n')
    if (markerIndex < 0) break
    const frame = remainder.slice(0, markerIndex)
    remainder = remainder.slice(markerIndex + 2)
    if (frame.trim()) frames.push(frame)
  }
  return { frames, remainder }
}

const parseSseEvent = (rawFrame) => {
  const lines = rawFrame.split(/\r?\n/)
  let eventName = 'message'
  const dataLines = []
  for (const line of lines) {
    if (line.startsWith('event:')) {
      eventName = line.slice(6).trim() || 'message'
      continue
    }
    if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trimStart())
    }
  }
  if (!dataLines.length) return null
  const payloadText = dataLines.join('\n')
  try {
    const payload = JSON.parse(payloadText)
    return { eventName, payload }
  } catch {
    return null
  }
}

const MessageBubble = ({ message }) => {
  const isAssistant = message.role === 'assistant'
  const [showAllSources, setShowAllSources] = useState(false)
  const [showDebug, setShowDebug] = useState(false)
  const linkOptions = {
    defaultProtocol: 'https',
    attributes: {
      target: '_blank',
      rel: 'noreferrer noopener',
      className: isAssistant
        ? 'text-blue-600 dark:text-blue-300 underline decoration-dotted hover:text-blue-500 dark:hover:text-blue-200'
        : 'text-indigo-100 underline decoration-dotted hover:text-white'
    }
  }

  const citations = message.meta?.citations || []
  const debug = message.meta?.debug || null
  const debugErrors = debug?.errors || []
  const debugWarnings = debug?.warnings || []
  const hasDebug = Boolean(debugErrors.length || debugWarnings.length)
  const visibleSources = showAllSources ? citations : citations.slice(0, SOURCE_LIMIT)
  const remainingSources = Math.max(citations.length - visibleSources.length, 0)

  return (
    <div className={`flex ${isAssistant ? 'justify-start' : 'justify-end'} mb-4`}>
      <div
        className={`max-w-[85%] rounded-lg px-3 py-2 text-sm shadow-sm ${
          isAssistant ? 'bg-card text-card-foreground border border-border' : 'bg-indigo-600 text-white'
        }`}
      >
        <div className="whitespace-pre-wrap leading-relaxed break-words">
          <Linkify options={linkOptions}>{message.content}</Linkify>
        </div>
        {citations.length ? (
          <div className={`mt-2 text-xs ${isAssistant ? 'text-muted-foreground' : 'text-indigo-100'}`}>
            <div className="flex items-center justify-between gap-3">
              <span>
                Sources {showAllSources ? `(${citations.length})` : `(showing ${visibleSources.length})`}
              </span>
              {citations.length > SOURCE_LIMIT ? (
                <button
                  type="button"
                  onClick={() => setShowAllSources((prev) => !prev)}
                  className={`text-xs font-medium ${
                    isAssistant
                      ? 'text-blue-600 hover:text-blue-500'
                      : 'text-indigo-100 hover:text-white'
                  }`}
                >
                  {showAllSources ? 'Hide' : `View all (+${remainingSources})`}
                </button>
              ) : null}
            </div>
            <div className="mt-1 break-all">
              {visibleSources.map((source, index) => (
                <span key={`${source}-${index}`}>
                  <Linkify options={linkOptions}>{source}</Linkify>
                  {index < visibleSources.length - 1 ? ', ' : ''}
                </span>
              ))}
            </div>
          </div>
        ) : null}
        {hasDebug && isAssistant ? (
          <div className="mt-2 text-xs text-muted-foreground">
            <div className="flex items-center justify-between gap-3">
              <span>Debug info</span>
              <button
                type="button"
                onClick={() => setShowDebug((prev) => !prev)}
                className="text-xs font-medium text-blue-600 hover:text-blue-500"
              >
                {showDebug ? 'Hide' : 'View'}
              </button>
            </div>
            {showDebug ? (
              <div className="mt-1 space-y-1">
                {debugErrors.map((item, index) => (
                  <div key={`debug-error-${index}`}>Error: {item}</div>
                ))}
                {debugWarnings.map((item, index) => (
                  <div key={`debug-warning-${index}`}>Warning: {item}</div>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  )
}

const AgentChat = ({ sessionId }) => {
  const [open, setOpen] = useState(false)
  const [messages, setMessages] = useState([initialMessage])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [digest, setDigest] = useState(null)
  const [digestOpen, setDigestOpen] = useState(false)
  const [digestError, setDigestError] = useState('')
  const [panelWidth, setPanelWidth] = useState(() => {
    if (typeof window === 'undefined') return 420
    const stored = window.localStorage.getItem('agent-chat-width')
    const parsed = stored ? parseInt(stored, 10) : 420
    return Number.isNaN(parsed) ? 420 : Math.min(Math.max(parsed, 360), 960)
  })
  const resizeDataRef = useRef({ startX: 0, startWidth: 420 })
  const resizingRef = useRef(false)
  const [resizing, setResizing] = useState(false)
  const scrollRef = useRef(null)
  const textareaRef = useRef(null)
  const fileInputRef = useRef(null)
  const [uploadingMemory, setUploadingMemory] = useState(false)

  useEffect(() => {
    if (open) {
      const handle = setTimeout(() => {
        scrollRef.current?.scrollIntoView({ behavior: 'smooth' })
      }, 50)
      return () => clearTimeout(handle)
    }
    return undefined
  }, [messages, open])

  useEffect(() => {
    if (typeof window === 'undefined') return undefined
    const clamped = Math.min(Math.max(panelWidth, 360), 960)
    window.localStorage.setItem('agent-chat-width', clamped.toString())
    return undefined
  }, [panelWidth])

  const handleResizeStart = (event) => {
    event.preventDefault()
    resizingRef.current = true
    setResizing(true)
    resizeDataRef.current = {
      startX: event.clientX,
      startWidth: panelWidth
    }
    window.addEventListener('pointermove', handleResizeMove)
    window.addEventListener('pointerup', handleResizeEnd)
  }

  const handleResizeMove = (event) => {
    if (!resizingRef.current) return
    const delta = resizeDataRef.current.startX - event.clientX
    const nextWidth = Math.min(Math.max(resizeDataRef.current.startWidth + delta, 360), 960)
    setPanelWidth(nextWidth)
  }

  const handleResizeEnd = () => {
    resizingRef.current = false
    setResizing(false)
    window.removeEventListener('pointermove', handleResizeMove)
    window.removeEventListener('pointerup', handleResizeEnd)
  }

  const handleDigestExport = async (format) => {
    if (!digest?.digest_id) return
    setDigestError(`Digest ${format} export is disabled until the backend route is restored.`)

    /*
    This export path is intentionally disabled for now. The frontend previously
    called POST /api/v1/agent/digest/export, but no matching backend route
    exists in the rental dashboard API.

    try {
      const response = await fetch(`${API_BASE}/api/v1/agent/digest/export`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          digest_id: digest.digest_id,
          format
        })
      })
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}))
        throw new Error(payload.error || `Export failed (${response.status})`)
      }
      const blob = await response.blob()
      const href = window.URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = href
      link.download = format === 'pdf' ? 'sentiment_digest.pdf' : 'sentiment_digest.md'
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.URL.revokeObjectURL(href)
    } catch (error) {
      setDigestError(error.message || 'Unable to export digest.')
    }
    */
  }

  const submitQuery = useCallback(
    async (event) => {
      event.preventDefault()
      const trimmed = input.trim()
      if (!trimmed || loading) return
      if (!sessionId) {
        setMessages((current) => [
          ...current,
          {
            role: 'assistant',
            content: 'Session not initialized yet. Refresh the page and try again.'
          }
        ])
        return
      }

      setMessages((current) => [...current, { role: 'user', content: trimmed }])
      setInput('')
      setLoading(true)
      setDigestError('')
      const assistantIndexRef = { current: -1 }

      try {
        setMessages((current) => {
          const next = [
            ...current,
            {
              role: 'assistant',
              content: '',
              meta: { citations: [], debug: { warnings: [] } }
            }
          ]
          assistantIndexRef.current = next.length - 1
          return next
        })

        const response = await fetch(`${API_BASE}/api/v1/agent/chat/stream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_id: sessionId,
            user_id: RAG_USER_ID,
            message: trimmed
          })
        })
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}))
          throw new Error(payload.error || `Request failed (${response.status})`)
        }
        if (!response.body) {
          throw new Error('Streaming response body unavailable')
        }

        const decoder = new TextDecoder()
        const reader = response.body.getReader()
        let buffer = ''
        let doneResponse = null
        const transientWarnings = []
        const transientErrors = []
        const seenProgress = new Set()

        while (true) {
          const { value, done } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          const parsed = parseSseFrames(buffer)
          buffer = parsed.remainder
          for (const rawFrame of parsed.frames) {
            const parsedEvent = parseSseEvent(rawFrame)
            if (!parsedEvent) continue
            const payload = parsedEvent.payload || {}
            const eventKind = payload.event || parsedEvent.eventName

            if (eventKind === 'assistant_delta') {
              const piece = String(payload.text || '')
              if (!piece) continue
              setMessages((current) => {
                const next = [...current]
                const index = assistantIndexRef.current
                if (index < 0 || index >= next.length) return current
                const target = next[index]
                if (!target || target.role !== 'assistant') return current
                next[index] = { ...target, content: `${target.content || ''}${piece}` }
                return next
              })
              continue
            }

            if (eventKind === 'assistant_block') {
              const blockType = String(payload.block_type || '').trim()
              const blockName = String(payload.name || '').trim()
              const marker = `${blockType}:${blockName}`
              if (!marker || seenProgress.has(marker)) continue
              seenProgress.add(marker)
              const progressText = blockName
                ? `\n[using tool: ${blockName}]`
                : blockType
                  ? `\n[step: ${blockType}]`
                  : ''
              if (!progressText) continue
              setMessages((current) => {
                const next = [...current]
                const index = assistantIndexRef.current
                if (index < 0 || index >= next.length) return current
                const target = next[index]
                if (!target || target.role !== 'assistant') return current
                next[index] = { ...target, content: `${target.content || ''}${progressText}` }
                return next
              })
              continue
            }

            if (eventKind === 'tool_result') {
              const toolName = String(payload.tool || '').trim() || 'tool'
              const ok = payload.ok !== false
              const marker = `tool_result:${toolName}:${ok ? 'ok' : 'error'}`
              if (seenProgress.has(marker)) continue
              seenProgress.add(marker)
              const progressText = ok
                ? `\n[tool completed: ${toolName}]`
                : `\n[tool error: ${toolName}]`
              setMessages((current) => {
                const next = [...current]
                const index = assistantIndexRef.current
                if (index < 0 || index >= next.length) return current
                const target = next[index]
                if (!target || target.role !== 'assistant') return current
                next[index] = { ...target, content: `${target.content || ''}${progressText}` }
                return next
              })
              continue
            }

            if (eventKind === 'tool_failure') {
              const toolName = String(payload.tool || '').trim() || 'tool'
              const marker = `tool_failure:${toolName}`
              if (seenProgress.has(marker)) continue
              seenProgress.add(marker)
              const progressText = `\n[tool failed: ${toolName}]`
              setMessages((current) => {
                const next = [...current]
                const index = assistantIndexRef.current
                if (index < 0 || index >= next.length) return current
                const target = next[index]
                if (!target || target.role !== 'assistant') return current
                next[index] = { ...target, content: `${target.content || ''}${progressText}` }
                return next
              })
              continue
            }

            if (eventKind === 'warning') {
              const warningText = String(payload.warning || '').trim()
              if (warningText) transientWarnings.push(warningText)
              continue
            }

            if (eventKind === 'error') {
              const errorText = String(payload.error || '').trim()
              if (errorText) transientErrors.push(errorText)
              continue
            }

            if (eventKind === 'done' && payload.response && typeof payload.response === 'object') {
              doneResponse = payload.response
            }
          }
        }

        if (!doneResponse) {
          throw new Error('Stream completed without a final response payload')
        }

        const debug = doneResponse.debug || null
        const debugErrors = [...(debug?.errors || []), ...transientErrors]
        const debugWarnings = [...(debug?.warnings || []), ...transientWarnings]
        const debugPayload = {
          ...(debug || {}),
          errors: debugErrors,
          warnings: debugWarnings
        }

        setMessages((current) => {
          const next = [...current]
          const index = assistantIndexRef.current
          if (index >= 0 && index < next.length && next[index]?.role === 'assistant') {
            next[index] = {
              role: 'assistant',
              content: doneResponse.reply || next[index].content || 'No response generated.',
              meta: {
                citations: doneResponse.citations || [],
                debug: debugPayload
              }
            }
            return next
          }
          next.push({
            role: 'assistant',
            content: doneResponse.reply || 'No response generated.',
            meta: {
              citations: doneResponse.citations || [],
              debug: debugPayload
            }
          })
          return next
        })

        if (doneResponse.digest?.digest_id) {
          setDigest(doneResponse.digest)
          setDigestOpen(true)
        }
      } catch (error) {
        const fallbackResponse = await fetch(`${API_BASE}/api/v1/agent/chat`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_id: sessionId,
            user_id: RAG_USER_ID,
            message: trimmed
          })
        }).catch(() => null)

        if (fallbackResponse && fallbackResponse.ok) {
          const data = await fallbackResponse.json().catch(() => ({}))
          const debug = data.debug || null
          const debugErrors = []
          if (data.error) debugErrors.push(data.error)
          if (error?.message) debugErrors.push(`stream_fallback: ${error.message}`)
          const debugPayload = debugErrors.length
            ? { ...(debug || {}), errors: [...(debug?.errors || []), ...debugErrors] }
            : debug
          setMessages((current) => {
            const next = [...current]
            const index = assistantIndexRef.current
            if (index >= 0 && index < next.length && next[index]?.role === 'assistant') {
              next[index] = {
                role: 'assistant',
                content: data.reply || 'No response generated.',
                meta: {
                  citations: data.citations || [],
                  debug: debugPayload
                }
              }
              return next
            }
            next.push({
              role: 'assistant',
              content: data.reply || 'No response generated.',
              meta: {
                citations: data.citations || [],
                debug: debugPayload
              }
            })
            return next
          })
          if (data.digest?.digest_id) {
            setDigest(data.digest)
            setDigestOpen(true)
          }
        } else {
          setMessages((current) => {
            const next = [...current]
            const index = assistantIndexRef.current
            const failureMessage = {
              role: 'assistant',
              content: 'I ran into an issue contacting the rental agent. Please try again.',
              meta: {
                debug: { errors: [error.message || 'Request failed.'], warnings: [] }
              }
            }
            if (index >= 0 && index < next.length && next[index]?.role === 'assistant') {
              next[index] = failureMessage
              return next
            }
            next.push(failureMessage)
            return next
          })
        }
      } finally {
        setLoading(false)
      }
    },
    [input, loading, sessionId]
  )

  const applySuggestedPrompt = useCallback((prompt) => {
    setInput(prompt)
    window.requestAnimationFrame(() => {
      textareaRef.current?.focus()
    })
  }, [])

  const handleComposerKeyDown = useCallback(
    (event) => {
      if (event.key !== 'Enter') return
      if (event.shiftKey) return
      if (event.nativeEvent?.isComposing) return
      submitQuery(event)
    },
    [submitQuery]
  )

  const openFilePicker = useCallback(() => {
    if (loading || uploadingMemory) return
    fileInputRef.current?.click()
  }, [loading, uploadingMemory])

  const handleMemoryFileSelected = useCallback(
    async (event) => {
      const file = event.target.files?.[0]
      event.target.value = ''
      if (!file) return
      if (!sessionId) {
        setMessages((current) => [
          ...current,
          {
            role: 'assistant',
            content: 'Session not initialized yet. Refresh and retry file upload.'
          }
        ])
        return
      }
      setUploadingMemory(true)
      try {
        const form = new FormData()
        form.append('file', file)
        form.append('user_id', RAG_USER_ID)
        form.append('source', 'chat_upload')
        const response = await fetch(`${API_BASE}/api/v1/memory/upload`, {
          method: 'POST',
          body: form
        })
        const data = await response.json().catch(() => ({}))
        if (!response.ok) {
          throw new Error(data.error || `Upload failed (${response.status})`)
        }
        const memory = data.memory || {}
        const chunkCount = Number(data.chunk_count || 0)
        const duplicate = Boolean(data.duplicate)
        const lines = [
          duplicate
            ? `Memory already exists and was linked: \`${memory.memory_id || 'unknown'}\`.`
            : `Memory uploaded: \`${memory.memory_id || 'unknown'}\`.`,
          `Title: ${memory.title || file.name}`,
          `Chunks indexed: ${chunkCount}`
        ]
        if (data.provider) {
          lines.push(`Embedding provider: ${data.provider}`)
        }
        setMessages((current) => [
          ...current,
          {
            role: 'assistant',
            content: lines.join('\n'),
            meta: { citations: ['/api/v1/memory/upload'] }
          }
        ])
      } catch (error) {
        setMessages((current) => [
          ...current,
          {
            role: 'assistant',
            content: `Memory upload failed: ${error.message || 'Unknown error'}`,
            meta: { debug: { errors: [error.message || 'Upload failed'], warnings: [] } }
          }
        ])
      } finally {
        setUploadingMemory(false)
      }
    },
    [sessionId]
  )

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button
          size="lg"
          className="fixed bottom-6 right-6 z-50 rounded-full shadow-lg bg-indigo-600 hover:bg-indigo-500 text-white flex items-center gap-2"
        >
          <MessageSquare className="h-4 w-4" />
          AI Chat
        </Button>
      </SheetTrigger>
      <SheetContent
        side="right"
        className="px-0 py-0 flex flex-col bg-background text-foreground transition-opacity duration-100"
        style={{ width: `${panelWidth}px`, maxWidth: '100%' }}
      >
        <SheetHeader className="px-6 pt-6 pb-4 border-b">
          <SheetTitle>Rental Agent</SheetTitle>
          <SheetDescription>Assistant for rental workflow insights and diagnostics.</SheetDescription>
        </SheetHeader>
        <div className="flex-1 flex flex-col overflow-hidden">
          <ScrollArea className="flex-1 px-6 py-4" style={{ height: '100%', overflow: 'auto' }}>
            <div className="h-full">
              {messages.map((message, index) => (
                <MessageBubble key={index} message={message} />
              ))}
              {digest ? (
                <div className="mt-4 rounded-lg border border-border/60 bg-card/60 p-3">
                  <div className="flex items-center justify-between">
                    <p className="text-sm font-semibold text-foreground flex items-center gap-2">
                      <FileText className="h-4 w-4" />
                      Digest ready
                    </p>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setDigestOpen((prev) => !prev)}
                    >
                      {digestOpen ? 'Hide' : 'View'}
                    </Button>
                  </div>
                  {digestOpen ? (
                    <div className="mt-2 space-y-2 text-xs text-muted-foreground whitespace-pre-wrap">
                      {digest.markdown}
                    </div>
                  ) : null}
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button size="sm" variant="outline" onClick={() => handleDigestExport('pdf')}>
                      Download PDF
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => handleDigestExport('markdown')}>
                      Download Markdown
                    </Button>
                  </div>
                  {digestError ? (
                    <p className="mt-2 text-xs text-destructive">{digestError}</p>
                  ) : null}
                </div>
              ) : null}
              <div ref={scrollRef} />
            </div>
          </ScrollArea>
          <div className="px-6 py-4 border-t space-y-3">
            <div className="flex flex-wrap gap-2">
              {SUGGESTED_PROMPTS.map((item) => (
                <Button
                  key={item.label}
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-7 px-2 text-xs"
                  onClick={() => applySuggestedPrompt(item.value)}
                  disabled={loading}
                >
                  {item.label}
                </Button>
              ))}
            </div>
            <form onSubmit={submitQuery} className="space-y-3">
              <input
                ref={fileInputRef}
                type="file"
                className="hidden"
                accept=".txt,.docx,.pdf,text/plain,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                onChange={handleMemoryFileSelected}
                disabled={loading || uploadingMemory}
              />
              <Textarea
                ref={textareaRef}
                value={input}
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={handleComposerKeyDown}
                placeholder="Ask about listings, comparisons, or pipeline health..."
                className="min-h-[96px] resize-none"
                disabled={loading}
              />
              <div className="flex items-center justify-between gap-2">
                <Button
                  type="button"
                  variant="outline"
                  onClick={openFilePicker}
                  disabled={loading || uploadingMemory}
                  className="min-w-[150px]"
                >
                  {uploadingMemory ? (
                    <span className="flex items-center gap-2">
                      <Loader2 className="h-4 w-4 animate-spin" />
                      Uploading
                    </span>
                  ) : (
                    <span className="flex items-center gap-2">
                      <Paperclip className="h-4 w-4" />
                      Add memory file
                    </span>
                  )}
                </Button>
                <Button type="submit" disabled={loading} className="min-w-[120px]">
                  {loading ? (
                    <span className="flex items-center gap-2">
                      <Loader2 className="h-4 w-4 animate-spin" />
                      Thinking
                    </span>
                  ) : (
                    <span className="flex items-center gap-2">
                      <span>Send</span>
                      <Send className="h-4 w-4" />
                    </span>
                  )}
                </Button>
              </div>
            </form>
          </div>
        </div>
        <div
          className="absolute inset-y-0 left-0 w-2 cursor-ew-resize select-none"
          onPointerDown={handleResizeStart}
          style={{ touchAction: 'none' }}
        >
          <div
            className={`h-full w-1 rounded-full bg-transparent transition ${
              resizing ? 'bg-primary/40' : 'hover:bg-primary/30'
            }`}
          />
        </div>
      </SheetContent>
    </Sheet>
  )
}

export default AgentChat
