import { useEffect, useMemo, useRef, useState } from 'react'
import {
  CalendarDays,
  CheckCircle2,
  ChevronRight,
  Database,
  ExternalLink,
  Loader2,
  Moon,
  Plus,
  RefreshCcw,
  Search,
  SlidersHorizontal,
  Sun,
  Trash2,
  Upload,
} from 'lucide-react'
import { Toaster, toast } from 'sonner'
import AgentChat from '@/components/AgentChat.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Checkbox } from '@/components/ui/checkbox.jsx'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog.jsx'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet.jsx'
import { Input } from '@/components/ui/input.jsx'
import { Label } from '@/components/ui/label.jsx'
import { ScrollArea } from '@/components/ui/scroll-area.jsx'
import { Separator } from '@/components/ui/separator.jsx'
import { Textarea } from '@/components/ui/textarea.jsx'
import { useLocalStorageState } from '@/hooks/useLocalStorageState.js'
import './App.css'

import {
  API_BASE,
  MEMORY_USER_ID,
  enableAgentChat,
  SETTINGS_KEY,
  COMPARE_DRAWER_KEY,
  suggestDebounceMs,
  defaultSettings,
  clampInteger,
  buildCaptureOverrides,
  getStoredTheme,
  formatTimestamp,
  formatMs,
  summarizeMissing,
  getUsdPriceValue,
  getUsdPriceLabel,
  getListingId,
  extractListingIdFromUrl,
  getListingUrl,
  getListingLocation,
  getListingRating,
  getListingCoordinates,
  getComparisonCoverage,
  getCaptureStageLabel,
  getCaptureStageBadgeVariant,
  formatReviewCoverage,
  needsFullReviews,
  getCaptureStageGuidance,
  getValidation,
  getValidationLabel,
  getMetricValue,
  getAmenitiesList,
  getPrimaryPhoto,
  formatJobLabel,
  requestJson,
} from '@/lib/dashboardUtils.js'
export default function App() {
  const [runs, setRuns] = useState([])
  const [selectedRunId, setSelectedRunId] = useState(null)
  const [summary, setSummary] = useState(null)
  const [listings, setListings] = useState([])
  const [selectedIds, setSelectedIds] = useState(new Set())
  const [searchTerm, setSearchTerm] = useState('')
  const [viewMode, setViewMode] = useState('search')
  const [ingestedSearchTerm, setIngestedSearchTerm] = useState('')
  const [ingestedSort, setIngestedSort] = useState('newest')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settings, setSettings] = useLocalStorageState(SETTINGS_KEY, defaultSettings, {
    deserialize: (stored) => ({ ...defaultSettings, ...JSON.parse(stored || '{}') }),
  })
  const [compareIds, setCompareIds] = useLocalStorageState('rental-compare', () => new Set(), {
    deserialize: (stored) => new Set(JSON.parse(stored || '[]')),
    serialize: (value) => JSON.stringify(Array.from(value || [])),
  })
  const selectedRunRef = useRef(null)
  const [loadingRuns, setLoadingRuns] = useState(false)
  const [loadingListings, setLoadingListings] = useState(false)
  const [ingesting, setIngesting] = useState(false)
  const [jobStats, setJobStats] = useState({ queued: 0, running: 0 })
  const [loadingJobs, setLoadingJobs] = useState(false)
  const [jobHistory, setJobHistory] = useState([])
  const [ingestedListings, setIngestedListings] = useState([])
  const [loadingIngested, setLoadingIngested] = useState(false)
  const [detailsOpen, setDetailsOpen] = useState(false)
  const [detailsListing, setDetailsListing] = useState(null)
  const [detailsReviews, setDetailsReviews] = useState([])
  const [detailsLoading, setDetailsLoading] = useState(false)
  const [detailsReviewsLoading, setDetailsReviewsLoading] = useState(false)
  const [llmSummary, setLlmSummary] = useState(null)
  const [llmSummaryStatus, setLlmSummaryStatus] = useState('idle')
  const [llmSummaryError, setLlmSummaryError] = useState(null)
  const [reviewExpanding, setReviewExpanding] = useState(() => new Set())
  const [compareOpen, setCompareOpen] = useState(false)
  const [compareDrawerWidth, setCompareDrawerWidth] = useState(() => {
    if (typeof window === 'undefined') return 680
    const stored = window.localStorage.getItem(COMPARE_DRAWER_KEY)
    const parsed = stored ? parseInt(stored, 10) : 680
    const clamped = Number.isNaN(parsed) ? 680 : Math.min(Math.max(parsed, 420), 980)
    return clamped
  })
  const compareResizeRef = useRef({ startX: 0, startWidth: 680 })
  const compareResizingRef = useRef(false)
  const [compareResizing, setCompareResizing] = useState(false)
  const [compareSummary, setCompareSummary] = useState(null)
  const [compareStatus, setCompareStatus] = useState('idle')
  const [compareError, setCompareError] = useState(null)
  const [amenitiesExpanded, setAmenitiesExpanded] = useState(false)
  const [theme, setTheme] = useState(() => getStoredTheme())
  const [railCollapsed, setRailCollapsed] = useState(false)
  const [actionsOpen, setActionsOpen] = useState(true)
  const [runsOpen, setRunsOpen] = useState(true)
  const [summaryOpen, setSummaryOpen] = useState(false)
  const [ingestedOpen, setIngestedOpen] = useState(true)
  const [jobsOpen, setJobsOpen] = useState(true)
  const [searchDialogOpen, setSearchDialogOpen] = useState(false)
  const [urlDialogOpen, setUrlDialogOpen] = useState(false)
  const [memoryDialogOpen, setMemoryDialogOpen] = useState(false)
  const [locationSuggestions, setLocationSuggestions] = useState([])
  const [locationSuggestOpen, setLocationSuggestOpen] = useState(false)
  const [locationSuggestLoading, setLocationSuggestLoading] = useState(false)
  const [locationSuggestError, setLocationSuggestError] = useState(null)
  const [perfMetrics, setPerfMetrics] = useState([])
  const [perfSummary, setPerfSummary] = useState(null)
  const [perfLoading, setPerfLoading] = useState(false)
  const [perfError, setPerfError] = useState(null)
  const suggestTimerRef = useRef(null)
  const [searchForm, setSearchForm] = useState({
    location: '',
    check_in: '',
    check_out: '',
    adults: 1,
    children: 0,
    infants: 0,
    pets: 0,
    min_price: '',
    max_price: '',
  })
  const [urlInput, setUrlInput] = useState('')
  const [searchSubmitting, setSearchSubmitting] = useState(false)
  const [urlSubmitting, setUrlSubmitting] = useState(false)
  const [memoryUploading, setMemoryUploading] = useState(false)
  const [memoryLoading, setMemoryLoading] = useState(false)
  const [memoryFiles, setMemoryFiles] = useState([])
  const [memoryTitle, setMemoryTitle] = useState('')
  const [memoryTags, setMemoryTags] = useState('')
  const [memoryFile, setMemoryFile] = useState(null)
  const jobIndexRef = useRef(new Map())
  const jobsInitializedRef = useRef(false)
  const checkInRef = useRef(null)
  const checkOutRef = useRef(null)

  const refreshRuns = async () => {
    setLoadingRuns(true)
    try {
      const data = await requestJson('/api/v1/search/runs?limit=50')
      setRuns(data.runs || [])
      if (!selectedRunRef.current && data.runs && data.runs.length > 0) {
        setSelectedRunId(data.runs[0].run_id)
      }
    } catch (err) {
      toast.error(`Failed to load search runs: ${err.message}`)
    } finally {
      setLoadingRuns(false)
    }
  }

  const refreshRunDetails = async (runId) => {
    if (!runId) return
    setLoadingListings(true)
    try {
      const [summaryData, listingsData] = await Promise.all([
        requestJson(`/api/v1/search/runs/${runId}/summary`),
        requestJson(`/api/v1/search/listings?run_id=${runId}&limit=200`),
      ])
      setSummary(summaryData.summary || null)
      setListings(listingsData.listings || [])
      setSelectedIds(new Set())
    } catch (err) {
      toast.error(`Failed to load run details: ${err.message}`)
    } finally {
      setLoadingListings(false)
    }
  }

  const refreshJobs = async () => {
    setLoadingJobs(true)
    try {
      const data = await requestJson('/api/v1/jobs?limit=50')
      const jobs = data.jobs || []
      const queued = jobs.filter((job) => job.status === 'queued').length
      const running = jobs.filter((job) => job.status === 'running').length
      setJobStats({ queued, running })
      setJobHistory(jobs)
      handleJobTransitions(jobs)
    } catch {
      setJobStats({ queued: 0, running: 0 })
    } finally {
      setLoadingJobs(false)
    }
  }

  const refreshPerfMetrics = async ({ silent = false } = {}) => {
    if (!silent) {
      setPerfLoading(true)
    }
    try {
      const data = await requestJson('/api/v1/metrics/jobs?limit=12&summary_limit=120')
      setPerfMetrics(Array.isArray(data.metrics) ? data.metrics : [])
      setPerfSummary(data.summary || null)
      setPerfError(null)
    } catch (err) {
      setPerfMetrics([])
      setPerfSummary(null)
      setPerfError(err.message || 'Failed to load performance metrics')
    } finally {
      setPerfLoading(false)
    }
  }

  const refreshIngestedListings = async () => {
    setLoadingIngested(true)
    try {
      const data = await requestJson('/api/v1/listings?limit=50')
      setIngestedListings(data.listings || [])
    } catch {
      setIngestedListings([])
    } finally {
      setLoadingIngested(false)
    }
  }

  const openListingDetails = async (listing) => {
    const listingId = getListingId(listing)
    if (!listingId) return
    setAmenitiesExpanded(false)
    setDetailsOpen(true)
    setDetailsLoading(true)
    setDetailsReviewsLoading(true)
    try {
      const data = await requestJson(`/api/v1/listings/${listingId}`)
      setDetailsListing(data.listing || null)
    } catch (err) {
      setDetailsListing(null)
      toast.error(`Failed to load listing details: ${err.message}`)
    } finally {
      setDetailsLoading(false)
    }
    try {
      const reviewsData = await requestJson(`/api/v1/reviews?listing_id=${listingId}&limit=10`)
      setDetailsReviews(reviewsData.reviews || [])
    } catch {
      setDetailsReviews([])
    } finally {
      setDetailsReviewsLoading(false)
    }
  }

  const refreshListingSummary = async (listingId, options = {}) => {
    if (!listingId) return
    const { silent = false, keepQueuedOn404 = false } = options
    setLlmSummary(null)
    setLlmSummaryError(null)
    if (!silent) {
      setLlmSummaryStatus('loading')
    }
    try {
      const res = await fetch(`${API_BASE}/api/v1/enrich/listings/${listingId}/summary`)
      if (res.status === 404) {
        if (!keepQueuedOn404) {
          setLlmSummaryStatus('idle')
        }
        return
      }
      if (!res.ok) {
        const message = await res.text()
        throw new Error(message || `Request failed (${res.status})`)
      }
      const data = await res.json()
      setLlmSummary(data.summary || null)
      setLlmSummaryStatus(data.summary ? 'ready' : 'idle')
    } catch (err) {
      setLlmSummaryError(err.message)
      setLlmSummaryStatus('error')
    }
  }

  const refreshListingDetails = async (listingId) => {
    if (!listingId) return
    setDetailsLoading(true)
    setDetailsReviewsLoading(true)
    try {
      const data = await requestJson(`/api/v1/listings/${listingId}`)
      setDetailsListing(data.listing || null)
    } catch (err) {
      toast.error(`Failed to refresh listing details: ${err.message}`)
    } finally {
      setDetailsLoading(false)
    }
    try {
      const reviewsData = await requestJson(`/api/v1/reviews?listing_id=${listingId}&limit=10`)
      setDetailsReviews(reviewsData.reviews || [])
    } catch {
      setDetailsReviews([])
    } finally {
      setDetailsReviewsLoading(false)
    }
    refreshListingSummary(listingId, { silent: true, keepQueuedOn404: true })
  }

  const generateListingSummary = async () => {
    const listingId = getListingId(detailsListing)
    if (!listingId) return
    setLlmSummaryError(null)
    setLlmSummaryStatus('submitting')
    try {
      const payload = {
        sync: true,
        review_limit: Number(settings.liteReviewCount) || 24,
      }
      if (settings.llmModelOverride) {
        payload.model = settings.llmModelOverride
      }
      const response = await requestJson(`/api/v1/enrich/listings/${listingId}/summary`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (response.summary) {
        setLlmSummary(response.summary)
        setLlmSummaryStatus('ready')
        toast.success('Summary generated.')
        return
      }
      setLlmSummaryStatus('queued')
      toast.message('Summary queued. This may take a moment.')
    } catch (err) {
      setLlmSummaryError(err.message)
      setLlmSummaryStatus('error')
      toast.error(`Summary failed: ${err.message}`)
    }
  }

  const handleJobTransitions = (jobs) => {
    if (!jobsInitializedRef.current) {
      const initial = new Map()
      jobs.forEach((job) => initial.set(job.job_id, job))
      jobIndexRef.current = initial
      jobsInitializedRef.current = true
      return
    }

    const prev = jobIndexRef.current
    const next = new Map()

    jobs.forEach((job) => {
      next.set(job.job_id, job)
      const previous = prev.get(job.job_id)
      if (previous && previous.status === job.status) {
        return
      }
      if (job.status === 'complete') {
        if (job.job_type === 'search') {
          toast.success('Search completed. Refreshing runs.')
          refreshRuns()
          if (job.result_ref) {
            // Move to the newly completed run so listings reflect the latest search.
            if (job.result_ref === selectedRunRef.current) {
              refreshRunDetails(job.result_ref)
            } else {
              setSelectedRunId(job.result_ref)
            }
          }
        } else if (job.job_type === 'listing_ingest') {
          toast.success('Listing ingest completed.')
          refreshIngestedListings()
          const activeListingId = getListingId(detailsListing)
          const jobListingId = job.result_ref
          if (activeListingId && jobListingId && String(activeListingId) === String(jobListingId)) {
            refreshListingDetails(jobListingId)
          } else if (detailsListing?.url && job.payload?.url) {
            const activeFromUrl = extractListingIdFromUrl(detailsListing.url)
            const jobFromUrl = extractListingIdFromUrl(job.payload.url)
            if (
              activeFromUrl &&
              jobFromUrl &&
              String(activeFromUrl) === String(jobFromUrl)
            ) {
              refreshListingDetails(activeFromUrl)
            }
          }
        } else if (job.job_type === 'listing_enrich') {
          toast.success('Listing summary ready.')
          const listingId = job.payload?.listing_id
          if (listingId && listingId === getListingId(detailsListing)) {
            refreshListingSummary(listingId)
          }
        } else if (job.job_type === 'listing_compare') {
          toast.success('Listing comparison ready.')
        } else {
          toast.success(`${job.job_type} completed.`)
        }
      } else if (job.status === 'failed') {
        toast.error(`${job.job_type} failed${job.error ? `: ${job.error}` : ''}`)
      }
    })

    jobIndexRef.current = next

    const activeListingId = getListingId(detailsListing)
    if (activeListingId && llmSummaryStatus !== 'ready') {
      const completedSummary = jobs.find(
        (job) =>
          job.job_type === 'listing_enrich' &&
          job.status === 'complete' &&
          String(job.payload?.listing_id) === String(activeListingId)
      )
      if (completedSummary) {
        refreshListingSummary(activeListingId)
      }
    }
  }

  useEffect(() => {
    refreshRuns()
  }, [])

  useEffect(() => {
    refreshRunDetails(selectedRunId)
  }, [selectedRunId])

  useEffect(() => {
    selectedRunRef.current = selectedRunId
  }, [selectedRunId])

  useEffect(() => {
    if (typeof window === 'undefined') return
    window.localStorage.setItem('rental-theme', theme)
    const body = document.body
    if (theme === 'dark') {
      body.classList.add('dark')
      body.classList.add('cam-dark')
    } else {
      body.classList.remove('dark')
      body.classList.remove('cam-dark')
    }
  }, [theme])

  useEffect(() => {
    if (!settingsOpen) return
    refreshPerfMetrics({ silent: false })
  }, [settingsOpen])

  useEffect(() => {
    if (!memoryDialogOpen) return
    refreshMemoryFiles({ silent: false })
  }, [memoryDialogOpen])

  useEffect(() => {
    if (!searchDialogOpen) {
      setLocationSuggestions([])
      setLocationSuggestOpen(false)
      setLocationSuggestLoading(false)
      setLocationSuggestError(null)
      if (suggestTimerRef.current) {
        clearTimeout(suggestTimerRef.current)
        suggestTimerRef.current = null
      }
      return
    }
    const query = searchForm.location.trim()
    if (query.length < 3) {
      setLocationSuggestions([])
      setLocationSuggestOpen(false)
      setLocationSuggestLoading(false)
      setLocationSuggestError(null)
      if (suggestTimerRef.current) {
        clearTimeout(suggestTimerRef.current)
        suggestTimerRef.current = null
      }
      return
    }
    if (suggestTimerRef.current) {
      clearTimeout(suggestTimerRef.current)
    }
    setLocationSuggestLoading(true)
    suggestTimerRef.current = setTimeout(async () => {
      try {
        const response = await fetch(
          `${API_BASE}/api/v1/geo/suggest?query=${encodeURIComponent(query)}`
        )
        if (!response.ok) {
          throw new Error(`Suggest failed: ${response.status}`)
        }
        const data = await response.json()
        const suggestions = Array.isArray(data.suggestions) ? data.suggestions : []
        setLocationSuggestions(suggestions)
        setLocationSuggestOpen(suggestions.length > 0)
        setLocationSuggestError(null)
      } catch (err) {
        setLocationSuggestions([])
        setLocationSuggestOpen(false)
        setLocationSuggestError(err.message || 'Suggest failed')
      } finally {
        setLocationSuggestLoading(false)
      }
    }, suggestDebounceMs)
    return () => {
      if (suggestTimerRef.current) {
        clearTimeout(suggestTimerRef.current)
        suggestTimerRef.current = null
      }
    }
  }, [searchDialogOpen, searchForm.location])

  useEffect(() => {
    if (typeof window === 'undefined') return
    const clamped = Math.min(Math.max(compareDrawerWidth, 420), 980)
    window.localStorage.setItem(COMPARE_DRAWER_KEY, clamped.toString())
  }, [compareDrawerWidth])

  useEffect(() => {
    setCompareSummary(null)
    setCompareStatus('idle')
    setCompareError(null)
  }, [compareIds])

  useEffect(() => {
    refreshJobs()
    const interval = setInterval(refreshJobs, 10000)
    return () => clearInterval(interval)
  }, [])

  useEffect(() => {
    refreshIngestedListings()
  }, [])

  useEffect(() => {
    const listingId = getListingId(detailsListing)
    if (!listingId) {
      setLlmSummary(null)
      setLlmSummaryStatus('idle')
      setLlmSummaryError(null)
      return
    }
    refreshListingSummary(listingId)
  }, [detailsListing?.id, detailsListing?.listing_id])

  useEffect(() => {
    const listingId = getListingId(detailsListing)
    if (!listingId || llmSummaryStatus !== 'queued') {
      return
    }
    let attempts = 0
    const timer = setInterval(() => {
      attempts += 1
      if (attempts > 12) {
        setLlmSummaryStatus('idle')
        clearInterval(timer)
        return
      }
      refreshListingSummary(listingId, { silent: true, keepQueuedOn404: true })
    }, 5000)
    return () => clearInterval(timer)
  }, [llmSummaryStatus, detailsListing?.id, detailsListing?.listing_id])

  const filteredListings = useMemo(() => {
    if (!searchTerm) return listings
    const needle = searchTerm.toLowerCase()
    return listings.filter((listing) => {
      return (
        listing.title?.toLowerCase().includes(needle) ||
        listing.location?.toLowerCase().includes(needle) ||
        listing.property_type?.toLowerCase().includes(needle)
      )
    })
  }, [listings, searchTerm])

  const filteredIngestedListings = useMemo(() => {
    if (!ingestedSearchTerm) return ingestedListings
    const needle = ingestedSearchTerm.toLowerCase()
    return ingestedListings.filter((listing) => {
      return (
        listing.title?.toLowerCase().includes(needle) ||
        listing.url?.toLowerCase().includes(needle) ||
        getListingLocation(listing)?.toLowerCase().includes(needle) ||
        listing.property_type?.toLowerCase().includes(needle)
      )
    })
  }, [ingestedListings, ingestedSearchTerm])

  const sortedIngestedListings = useMemo(() => {
    const list = [...filteredIngestedListings]
    const compareNumbers = (a, b) => (a === null || a === undefined ? -1 : a) - (b === null || b === undefined ? -1 : b)
    list.sort((a, b) => {
      if (ingestedSort === 'newest') return (b.captured_at || 0) - (a.captured_at || 0)
      if (ingestedSort === 'oldest') return (a.captured_at || 0) - (b.captured_at || 0)
      if (ingestedSort === 'rating') return compareNumbers(getListingRating(b), getListingRating(a))
      if (ingestedSort === 'price_low') {
        return compareNumbers(
          getUsdPriceValue(a, settings.priceDisplay).value,
          getUsdPriceValue(b, settings.priceDisplay).value
        )
      }
      if (ingestedSort === 'price_high') {
        return compareNumbers(
          getUsdPriceValue(b, settings.priceDisplay).value,
          getUsdPriceValue(a, settings.priceDisplay).value
        )
      }
      return 0
    })
    return list
  }, [filteredIngestedListings, ingestedSort, settings.priceDisplay])

  const ingestedIdSet = useMemo(() => {
    const ids = new Set()
    ingestedListings.forEach((listing) => {
      const listingId = getListingId(listing)
      if (listingId) {
        ids.add(String(listingId))
      }
    })
    return ids
  }, [ingestedListings])

  const compareListingMap = useMemo(() => {
    const map = new Map()
    ingestedListings.forEach((listing) => {
      const listingId = getListingId(listing)
      if (listingId) {
        map.set(String(listingId), listing)
      }
    })
    return map
  }, [ingestedListings])

  const selectedCompareListings = useMemo(() => {
    const items = []
    compareIds.forEach((id) => {
      const listing = compareListingMap.get(String(id))
      if (listing) items.push(listing)
    })
    return items
  }, [compareIds, compareListingMap])

  const compareNeedsFull = useMemo(
    () => selectedCompareListings.some((listing) => needsFullReviews(listing)),
    [selectedCompareListings]
  )
  const compareMinCoverageRatio = useMemo(() => {
    const percent = clampInteger(settings.minCompareCoveragePercent, 1, 100, 50) || 50
    return percent / 100
  }, [settings.minCompareCoveragePercent])
  const compareCoverageViolations = useMemo(() => {
    if (!settings.requireMinCompareCoverage) return []
    const reviewLimit = Number(settings.liteReviewCount) || 24
    return selectedCompareListings
      .map((listing) => {
        const snapshot = getComparisonCoverage(listing, reviewLimit)
        return { listing, ...snapshot }
      })
      .filter((item) => item.coverage + 1e-9 < compareMinCoverageRatio)
  }, [
    compareMinCoverageRatio,
    selectedCompareListings,
    settings.liteReviewCount,
    settings.requireMinCompareCoverage,
  ])

  const amenitiesList = useMemo(() => getAmenitiesList(detailsListing), [detailsListing])
  const detailsStageLabel = useMemo(() => getCaptureStageLabel(detailsListing), [detailsListing])
  const detailsStageGuidance = useMemo(() => getCaptureStageGuidance(detailsListing), [detailsListing])
  const detailsCoverageLabel = useMemo(
    () => formatReviewCoverage(detailsListing),
    [detailsListing]
  )
  const detailsNeedsFull = useMemo(
    () => needsFullReviews(detailsListing),
    [detailsListing]
  )
  const detailsReviewExpanding = useMemo(() => {
    const key = String(getListingId(detailsListing) || detailsListing?.url || '')
    return key ? reviewExpanding.has(key) : false
  }, [detailsListing, reviewExpanding])

  const toggleSelection = (listingId) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(listingId)) {
        next.delete(listingId)
      } else {
        next.add(listingId)
      }
      return next
    })
  }

  const selectAll = () => {
    setSelectedIds(new Set(filteredListings.map((listing) => listing.id)))
  }

  const clearSelection = () => setSelectedIds(new Set())

  const toggleCompareSelection = (listingId) => {
    if (!listingId) return
    setCompareIds((prev) => {
      const next = new Set(prev)
      if (next.has(listingId)) {
        next.delete(listingId)
        return next
      }
      const maxCompare = Number(settings.compareMax) || 6
      if (next.size >= maxCompare) {
        toast.error(`You can compare up to ${maxCompare} listings.`)
        return next
      }
      next.add(listingId)
      return next
    })
  }

  const clearCompare = () => {
    setCompareIds(new Set())
    setCompareSummary(null)
    setCompareStatus('idle')
    setCompareError(null)
  }

  const setReviewExpandState = (listingKey, active) => {
    if (!listingKey) return
    setReviewExpanding((prev) => {
      const next = new Set(prev)
      if (active) {
        next.add(listingKey)
      } else {
        next.delete(listingKey)
      }
      return next
    })
  }

  const queueFullReviews = async (listing, options = {}) => {
    const { silent = false } = options
    const url = getListingUrl(listing)
    const listingKey = String(getListingId(listing) || url || '')
    if (!url) {
      if (!silent) toast.error('Missing listing URL for full review capture.')
      return
    }
    setReviewExpandState(listingKey, true)
    try {
      await requestJson('/api/v1/listings/ingest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          url,
          review_mode: 'full',
          review_only: true,
          force: true,
          ...buildCaptureOverrides(settings),
        }),
      })
      if (!silent) {
        toast.success('Queued full review capture.')
      }
      refreshJobs()
    } catch (err) {
      if (!silent) {
        toast.error(`Full review capture failed: ${err.message}`)
      }
    } finally {
      setReviewExpandState(listingKey, false)
    }
  }

  const upgradeCompareReviews = async () => {
    const targets = selectedCompareListings.filter((listing) => needsFullReviews(listing))
    if (!targets.length) return
    for (const listing of targets) {
      // Queue sequentially to avoid burst.
      await queueFullReviews(listing, { silent: true })
    }
    toast.message(`Queued full reviews for ${targets.length} listing${targets.length === 1 ? '' : 's'}.`)
  }

  const ingestSelected = async () => {
    if (!selectedRunId || selectedIds.size === 0) return
    setIngesting(true)
    try {
      const reviewMode = settings.reviewMode || 'lite'
      const payload = {
        run_id: selectedRunId,
        listing_ids: Array.from(selectedIds),
        review_mode: reviewMode,
        ...buildCaptureOverrides(settings),
      }
      if (reviewMode === 'lite') {
        payload.review_limit = Number(settings.liteReviewCount) || 24
      }
      const response = await requestJson('/api/v1/search/ingest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      toast.success(`Queued ${response.jobs?.length || 0} listing ingests`)
    } catch (err) {
      toast.error(`Ingest failed: ${err.message}`)
    } finally {
      setIngesting(false)
    }
  }

  const generateComparison = async () => {
    if (compareIds.size < 2) {
      toast.error('Select at least 2 listings to compare.')
      return
    }
    if (settings.requireMinCompareCoverage && compareCoverageViolations.length > 0) {
      const first = compareCoverageViolations[0]
      const listingLabel =
        first?.listing?.title || getListingId(first?.listing) || 'Selected listing'
      const requiredPercent = Math.round(compareMinCoverageRatio * 100)
      toast.error(
        `${listingLabel} is below minimum comparison coverage (${requiredPercent}% of target sample).`
      )
      setCompareStatus('error')
      setCompareError('Comparison blocked by minimum coverage setting. Fetch full reviews and retry.')
      return
    }
    setCompareStatus('loading')
    setCompareError(null)
    try {
      const minCoverageRatio = compareMinCoverageRatio
      const response = await requestJson('/api/v1/enrich/compare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          listing_ids: Array.from(compareIds),
          sync: true,
          review_limit: Number(settings.liteReviewCount) || 24,
          require_min_coverage: Boolean(settings.requireMinCompareCoverage),
          min_review_coverage: Number.isFinite(minCoverageRatio) ? minCoverageRatio : undefined,
          model: settings.llmModelOverride || undefined,
        }),
      })
      if (response.summary) {
        setCompareSummary(response.summary)
        setCompareStatus('ready')
        toast.success('Comparison generated.')
        return
      }
      setCompareStatus('idle')
      toast.message('Comparison queued.')
    } catch (err) {
      const errorPayload = err?.payload && typeof err.payload === 'object' ? err.payload : null
      if (errorPayload?.code === 'comparison_coverage_blocked') {
        const violations = Array.isArray(errorPayload.violations) ? errorPayload.violations : []
        const preview = violations
          .slice(0, 3)
          .map((item) => {
            const title = item?.title || item?.listing_id || 'Listing'
            const captured = Number(item?.captured || 0)
            const target = Number(item?.target || 0)
            return `${title} (${captured}/${target})`
          })
          .join(', ')
        const message = preview
          ? `Coverage policy blocked comparison: ${preview}`
          : 'Coverage policy blocked comparison.'
        setCompareError(message)
        setCompareStatus('error')
        toast.error(message)
        return
      }
      setCompareError(err.message)
      setCompareStatus('error')
      toast.error(`Comparison failed: ${err.message}`)
    }
  }

  const submitSearch = async () => {
    if (!searchForm.location.trim()) {
      toast.error('Location is required')
      return
    }
    setSearchSubmitting(true)
    try {
      const payload = {
        location: searchForm.location.trim(),
        ...buildCaptureOverrides(settings, { includeReviews: false }),
      }
      ;[
        'check_in',
        'check_out',
        'adults',
        'children',
        'infants',
        'pets',
        'min_price',
        'max_price',
      ].forEach((key) => {
        const value = searchForm[key]
        if (value !== '' && value !== null && value !== undefined) {
          payload[key] = value
        }
      })
      await requestJson('/api/v1/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      toast.success('Search queued. Refresh runs to see results.')
      setSearchDialogOpen(false)
      refreshRuns()
      refreshJobs()
    } catch (err) {
      toast.error(`Search failed: ${err.message}`)
    } finally {
      setSearchSubmitting(false)
    }
  }

  const submitUrls = async () => {
    const urls = urlInput
      .split(/[\n,]+/)
      .map((item) => item.trim())
      .filter(Boolean)
    if (!urls.length) {
      toast.error('Add at least one listing URL')
      return
    }
    setUrlSubmitting(true)
    try {
      const reviewMode = settings.reviewMode || 'lite'
      const results = []
      for (const url of urls) {
        const response = await requestJson('/api/v1/listings/ingest', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            url,
            review_mode: reviewMode,
            review_limit: reviewMode === 'lite' ? Number(settings.liteReviewCount) || 24 : undefined,
            ...buildCaptureOverrides(settings),
          }),
        })
        results.push(response.job?.job_id)
      }
      toast.success(`Queued ${results.length} listing ingests`)
      setUrlDialogOpen(false)
      setUrlInput('')
      refreshJobs()
    } catch (err) {
      toast.error(`URL ingest failed: ${err.message}`)
    } finally {
      setUrlSubmitting(false)
    }
  }

  const refreshMemoryFiles = async ({ silent = false } = {}) => {
    if (!silent) setMemoryLoading(true)
    try {
      const data = await requestJson(
        `/api/v1/memory/files?user_id=${encodeURIComponent(MEMORY_USER_ID)}&limit=100`
      )
      setMemoryFiles(Array.isArray(data.memories) ? data.memories : [])
    } catch (err) {
      if (!silent) toast.error(`Failed to load memory files: ${err.message}`)
    } finally {
      if (!silent) setMemoryLoading(false)
    }
  }

  const submitMemoryUpload = async () => {
    if (!memoryFile) {
      toast.error('Select a .txt, .docx, or .pdf file first.')
      return
    }
    setMemoryUploading(true)
    try {
      const form = new FormData()
      form.append('file', memoryFile)
      form.append('user_id', MEMORY_USER_ID)
      if (memoryTitle.trim()) form.append('title', memoryTitle.trim())
      if (memoryTags.trim()) form.append('tags', memoryTags.trim())
      form.append('source', 'dashboard_memory_modal')
      const response = await fetch(`${API_BASE}/api/v1/memory/upload`, {
        method: 'POST',
        body: form,
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(payload.error || `Upload failed (${response.status})`)
      }
      const chunkCount = Number(payload.chunk_count || 0)
      toast.success(`Memory indexed (${chunkCount} chunks)`)
      setMemoryFile(null)
      setMemoryTitle('')
      setMemoryTags('')
      await refreshMemoryFiles({ silent: true })
    } catch (err) {
      toast.error(`Memory upload failed: ${err.message}`)
    } finally {
      setMemoryUploading(false)
    }
  }

  const deleteMemoryFile = async (memoryId) => {
    if (!memoryId) return
    try {
      await requestJson(
        `/api/v1/memory/files/${encodeURIComponent(memoryId)}?user_id=${encodeURIComponent(MEMORY_USER_ID)}`,
        { method: 'DELETE' }
      )
      toast.success('Memory deleted')
      await refreshMemoryFiles({ silent: true })
    } catch (err) {
      toast.error(`Delete failed: ${err.message}`)
    }
  }

  const railWidthClass = railCollapsed ? 'w-full lg:w-[84px]' : 'w-full lg:w-[340px]'
  const openDatePicker = (ref) => {
    const input = ref?.current
    if (!input) return
    if (typeof input.showPicker === 'function') {
      try {
        input.showPicker()
      } catch {
        // Some browsers block programmatic picker.
      }
    }
  }

  const handleCompareResizeStart = (event) => {
    event.preventDefault()
    compareResizingRef.current = true
    setCompareResizing(true)
    compareResizeRef.current = {
      startX: event.clientX,
      startWidth: compareDrawerWidth,
    }
    window.addEventListener('pointermove', handleCompareResizeMove)
    window.addEventListener('pointerup', handleCompareResizeEnd)
  }

  const handleCompareResizeMove = (event) => {
    if (!compareResizingRef.current) return
    const delta = compareResizeRef.current.startX - event.clientX
    const nextWidth = Math.min(Math.max(compareResizeRef.current.startWidth + delta, 420), 980)
    setCompareDrawerWidth(nextWidth)
  }

  const handleCompareResizeEnd = () => {
    compareResizingRef.current = false
    setCompareResizing(false)
    window.removeEventListener('pointermove', handleCompareResizeMove)
    window.removeEventListener('pointerup', handleCompareResizeEnd)
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      <Toaster richColors position="top-right" />
      <Dialog open={searchDialogOpen} onOpenChange={setSearchDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>New search</DialogTitle>
            <DialogDescription>Enter location and optional filters.</DialogDescription>
          </DialogHeader>
          <div className="grid gap-3">
            <Label>Location</Label>
            <div className="relative">
              <Input
                placeholder="Location (required)"
                value={searchForm.location}
                onChange={(event) => setSearchForm((prev) => ({ ...prev, location: event.target.value }))}
                onFocus={() => locationSuggestions.length > 0 && setLocationSuggestOpen(true)}
                onBlur={() => setTimeout(() => setLocationSuggestOpen(false), 150)}
              />
              {locationSuggestLoading && (
                <div className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-muted-foreground">
                  Loading...
                </div>
              )}
              {locationSuggestOpen && (
                <div className="absolute z-50 mt-2 max-h-52 w-full overflow-auto rounded-md border border-border bg-background text-sm shadow-lg">
                  {locationSuggestions.map((suggestion) => (
                    <button
                      type="button"
                      key={suggestion.place_id || suggestion.label}
                      className="block w-full px-3 py-2 text-left hover:bg-muted/70"
                      onMouseDown={(event) => event.preventDefault()}
                      onClick={() => {
                        setSearchForm((prev) => ({ ...prev, location: suggestion.label }))
                        setLocationSuggestOpen(false)
                      }}
                    >
                      {suggestion.label}
                    </button>
                  ))}
                  {locationSuggestions.length === 0 && (
                    <div className="px-3 py-2 text-xs text-muted-foreground">
                      No suggestions
                    </div>
                  )}
                </div>
              )}
              {locationSuggestError && !locationSuggestLoading && (
                <div className="mt-1 text-xs text-muted-foreground">{locationSuggestError}</div>
              )}
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label>Check-in</Label>
                <Input
                  type="date"
                  ref={checkInRef}
                  value={searchForm.check_in}
                  onFocus={() => openDatePicker(checkInRef)}
                  onClick={() => openDatePicker(checkInRef)}
                  onChange={(event) => setSearchForm((prev) => ({ ...prev, check_in: event.target.value }))}
                />
              </div>
              <div className="space-y-1">
                <Label>Check-out</Label>
                <Input
                  type="date"
                  ref={checkOutRef}
                  value={searchForm.check_out}
                  onFocus={() => openDatePicker(checkOutRef)}
                  onClick={() => openDatePicker(checkOutRef)}
                  onChange={(event) => setSearchForm((prev) => ({ ...prev, check_out: event.target.value }))}
                />
              </div>
            </div>
            <div className="grid gap-3 sm:grid-cols-4">
              <div className="space-y-1">
                <Label>Adults</Label>
                <Input
                  type="number"
                  min="1"
                  value={searchForm.adults}
                  onChange={(event) =>
                    setSearchForm((prev) => ({ ...prev, adults: Number(event.target.value) || 0 }))
                  }
                />
              </div>
              <div className="space-y-1">
                <Label>Children</Label>
                <Input
                  type="number"
                  min="0"
                  value={searchForm.children}
                  onChange={(event) =>
                    setSearchForm((prev) => ({ ...prev, children: Number(event.target.value) || 0 }))
                  }
                />
              </div>
              <div className="space-y-1">
                <Label>Infants</Label>
                <Input
                  type="number"
                  min="0"
                  value={searchForm.infants}
                  onChange={(event) =>
                    setSearchForm((prev) => ({ ...prev, infants: Number(event.target.value) || 0 }))
                  }
                />
              </div>
              <div className="space-y-1">
                <Label>Pets</Label>
                <Input
                  type="number"
                  min="0"
                  value={searchForm.pets}
                  onChange={(event) =>
                    setSearchForm((prev) => ({ ...prev, pets: Number(event.target.value) || 0 }))
                  }
                />
              </div>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label>Min price</Label>
                <Input
                  type="number"
                  min="0"
                  value={searchForm.min_price}
                  onChange={(event) => setSearchForm((prev) => ({ ...prev, min_price: event.target.value }))}
                />
              </div>
              <div className="space-y-1">
                <Label>Max price</Label>
                <Input
                  type="number"
                  min="0"
                  value={searchForm.max_price}
                  onChange={(event) => setSearchForm((prev) => ({ ...prev, max_price: event.target.value }))}
                />
              </div>
            </div>
            <Button onClick={submitSearch} disabled={searchSubmitting}>
              {searchSubmitting ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Run search'}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
        <Dialog open={urlDialogOpen} onOpenChange={setUrlDialogOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Ingest listing URLs</DialogTitle>
              <DialogDescription>Paste one URL per line.</DialogDescription>
            </DialogHeader>
            <div className="grid gap-3">
              <Textarea
                value={urlInput}
                onChange={(event) => setUrlInput(event.target.value)}
                placeholder="https://www.airbnb.com/rooms/..."
              />
              <Button onClick={submitUrls} disabled={urlSubmitting}>
                {urlSubmitting ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Queue ingests'}
              </Button>
            </div>
          </DialogContent>
        </Dialog>
        <Dialog open={memoryDialogOpen} onOpenChange={setMemoryDialogOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Trip memory store</DialogTitle>
              <DialogDescription>
                Upload past trip files (.txt, .docx, .pdf) to build personality memory context.
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-3">
              <div className="grid gap-2">
                <Label>Title (optional)</Label>
                <Input
                  value={memoryTitle}
                  onChange={(event) => setMemoryTitle(event.target.value)}
                  placeholder="Adirondacks 2023 itinerary"
                />
              </div>
              <div className="grid gap-2">
                <Label>Tags (optional, comma-separated)</Label>
                <Input
                  value={memoryTags}
                  onChange={(event) => setMemoryTags(event.target.value)}
                  placeholder="hiking, couple, roadtrip"
                />
              </div>
              <div className="grid gap-2">
                <Label>File</Label>
                <Input
                  type="file"
                  accept=".txt,.docx,.pdf,text/plain,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                  onChange={(event) => setMemoryFile(event.target.files?.[0] || null)}
                />
              </div>
              <div className="flex items-center gap-2">
                <Button onClick={submitMemoryUpload} disabled={memoryUploading}>
                  {memoryUploading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Upload className="mr-2 h-4 w-4" />}
                  Upload memory
                </Button>
                <Button variant="outline" onClick={() => refreshMemoryFiles({ silent: false })} disabled={memoryLoading}>
                  {memoryLoading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCcw className="mr-2 h-4 w-4" />}
                  Refresh
                </Button>
              </div>
              <Separator />
              <ScrollArea className="h-56 rounded-md border border-border/60 p-2">
                <div className="space-y-2 text-sm">
                  {memoryLoading && (
                    <div className="flex items-center gap-2 text-xs text-muted-foreground">
                      <Loader2 className="h-3 w-3 animate-spin" /> Loading memory files...
                    </div>
                  )}
                  {!memoryLoading && memoryFiles.length === 0 && (
                    <div className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
                      No memory files uploaded yet.
                    </div>
                  )}
                  {memoryFiles.map((item) => (
                    <div
                      key={item.memory_id}
                      className="flex items-start justify-between gap-3 rounded-md border border-border/60 p-2"
                    >
                      <div className="min-w-0">
                        <p className="truncate text-xs font-semibold">{item.title || item.filename || item.memory_id}</p>
                        <p className="truncate text-[11px] text-muted-foreground">
                          {item.filename || item.source_type || 'memory'}
                        </p>
                        <div className="mt-1 flex flex-wrap gap-1">
                          {(item.tags || []).slice(0, 4).map((tag) => (
                            <Badge key={`${item.memory_id}-${tag}`} variant="outline" className="text-[10px]">
                              {tag}
                            </Badge>
                          ))}
                          <Badge variant="outline" className="text-[10px]">{item.status || 'ready'}</Badge>
                        </div>
                      </div>
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => deleteMemoryFile(item.memory_id)}
                        title="Delete memory"
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  ))}
                </div>
              </ScrollArea>
            </div>
          </DialogContent>
        </Dialog>
        <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Settings</DialogTitle>
              <DialogDescription>Tune capture and comparison defaults.</DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 text-sm">
              <div className="grid gap-2">
                <Label>Default review capture mode</Label>
                <select
                  className="h-10 rounded-md border border-border bg-background px-3"
                  value={settings.reviewMode}
                  onChange={(event) =>
                    setSettings((prev) => ({ ...prev, reviewMode: event.target.value }))
                  }
                >
                  <option value="none">None</option>
                  <option value="lite">Lite (sample)</option>
                  <option value="full">Full</option>
                </select>
              </div>
              <div className="grid gap-2">
                <Label>Lite review sample size</Label>
                <Input
                  type="number"
                  min="6"
                  max="50"
                  value={settings.liteReviewCount}
                  onChange={(event) =>
                    setSettings((prev) => ({
                      ...prev,
                      liteReviewCount: Number(event.target.value) || 24,
                    }))
                  }
                />
              </div>
              <div className="flex items-center justify-between">
                <Label>Show compare disclaimer for lite reviews</Label>
                <Checkbox
                  checked={settings.compareDisclaimer}
                  onCheckedChange={(checked) =>
                    setSettings((prev) => ({ ...prev, compareDisclaimer: Boolean(checked) }))
                  }
                />
              </div>
              <div className="flex items-center justify-between">
                <Label>Require minimum review coverage to compare</Label>
                <Checkbox
                  checked={settings.requireMinCompareCoverage}
                  onCheckedChange={(checked) =>
                    setSettings((prev) => ({ ...prev, requireMinCompareCoverage: Boolean(checked) }))
                  }
                />
              </div>
              <div className="grid gap-2">
                <Label>Minimum comparison coverage (% of target sample)</Label>
                <Input
                  type="number"
                  min="1"
                  max="100"
                  value={settings.minCompareCoveragePercent}
                  disabled={!settings.requireMinCompareCoverage}
                  onChange={(event) =>
                    setSettings((prev) => ({
                      ...prev,
                      minCompareCoveragePercent:
                        clampInteger(event.target.value, 1, 100, 50) || 50,
                    }))
                  }
                />
                <p className="text-xs text-muted-foreground">
                  Coverage target uses min(total reviews, lite sample size) per listing.
                </p>
              </div>
              <div className="grid gap-2">
                <Label>Max listings to compare (2-6)</Label>
                <Input
                  type="number"
                  min="2"
                  max="6"
                  value={settings.compareMax}
                  onChange={(event) =>
                    setSettings((prev) => ({
                      ...prev,
                      compareMax: Math.min(6, Math.max(2, Number(event.target.value) || 6)),
                    }))
                  }
                />
              </div>
              <div className="grid gap-2">
                <Label>Price display</Label>
                <select
                  className="h-10 rounded-md border border-border bg-background px-3"
                  value={settings.priceDisplay}
                  onChange={(event) =>
                    setSettings((prev) => ({ ...prev, priceDisplay: event.target.value }))
                  }
                >
                  <option value="total">Total (stay)</option>
                  <option value="nightly">Nightly</option>
                </select>
                <p className="text-xs text-muted-foreground">
                  Prices are displayed in USD using the latest FX rate.
                </p>
              </div>
              <Separator />
              <div className="grid gap-2">
                <Label>Capture timeout (ms)</Label>
                <Input
                  type="number"
                  min="30000"
                  value={settings.captureTimeoutMs}
                  onChange={(event) =>
                    setSettings((prev) => ({
                      ...prev,
                      captureTimeoutMs: Number(event.target.value) || 180000,
                    }))
                  }
                />
              </div>
              <div className="grid gap-2">
                <Label>Full review pagination passes</Label>
                <Input
                  type="number"
                  min="2"
                  value={settings.reviewPaginationPasses}
                  onChange={(event) =>
                    setSettings((prev) => ({
                      ...prev,
                      reviewPaginationPasses: Number(event.target.value) || 8,
                    }))
                  }
                />
              </div>
              <div className="grid gap-2">
                <Label>LLM model override (optional)</Label>
                <Input
                  value={settings.llmModelOverride}
                  onChange={(event) =>
                    setSettings((prev) => ({ ...prev, llmModelOverride: event.target.value }))
                  }
                  placeholder="gpt-5-mini"
                />
              </div>
              <Separator />
              <div className="grid gap-3">
                <div className="flex items-center justify-between">
                  <Label>Debug performance (recent jobs)</Label>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => refreshPerfMetrics({ silent: false })}
                    disabled={perfLoading}
                  >
                    {perfLoading ? <Loader2 className="mr-2 h-3 w-3 animate-spin" /> : null}
                    Refresh
                  </Button>
                </div>
                {perfError && (
                  <p className="text-xs text-destructive">{perfError}</p>
                )}
                {!perfError && (
                  <div className="grid gap-2 rounded-md border border-border/60 bg-background/50 p-3 text-xs">
                    <div className="flex flex-wrap gap-2">
                      <Badge variant="outline">
                        Jobs: {perfSummary?.count ?? 0}
                      </Badge>
                      <Badge variant="outline">
                        Avg total: {formatMs(perfSummary?.averages?.avg_job_total_ms)}
                      </Badge>
                      <Badge variant="outline">
                        Avg nav: {formatMs(perfSummary?.averages?.avg_navigation_ms)}
                      </Badge>
                      <Badge variant="outline">
                        Avg parse: {formatMs(perfSummary?.averages?.avg_parse_ms)}
                      </Badge>
                      <Badge variant="outline">
                        Avg persist: {formatMs(perfSummary?.averages?.avg_persist_ms)}
                      </Badge>
                    </div>
                    {(perfMetrics || []).slice(0, 5).map((entry) => (
                      <div
                        key={entry.metric_id || entry.job_id}
                        className="flex flex-wrap items-center gap-2 border-t border-border/60 pt-2"
                      >
                        <Badge variant="outline">{entry.job_type || 'job'}</Badge>
                        <Badge variant={entry.status === 'failed' ? 'destructive' : 'secondary'}>
                          {entry.status || 'n/a'}
                        </Badge>
                        <span>Total {formatMs(getMetricValue(entry, 'job_total_ms'))}</span>
                        <span>Nav {formatMs(getMetricValue(entry, 'navigation_ms'))}</span>
                        <span>Parse {formatMs(getMetricValue(entry, 'parse_ms'))}</span>
                        <span>Persist {formatMs(getMetricValue(entry, 'persist_ms'))}</span>
                      </div>
                    ))}
                    {!perfLoading && (!perfMetrics || perfMetrics.length === 0) && (
                      <p className="text-muted-foreground">No metrics recorded yet.</p>
                    )}
                  </div>
                )}
              </div>
            </div>
          </DialogContent>
        </Dialog>
          <Sheet open={compareOpen} onOpenChange={setCompareOpen}>
            <SheetContent
              className="w-full sm:max-w-none"
              style={{ width: compareDrawerWidth, maxWidth: '100vw' }}
            >
              <div
                className="absolute inset-y-0 left-0 hidden w-2 cursor-ew-resize select-none sm:block"
                onPointerDown={handleCompareResizeStart}
                style={{ touchAction: 'none' }}
              >
                <div
                  className={`h-full w-1 rounded-full bg-transparent transition ${
                    compareResizing ? 'bg-primary/40' : 'hover:bg-primary/30'
                  }`}
                />
              </div>
            <SheetHeader>
              <SheetTitle>Listing comparison</SheetTitle>
              <SheetDescription>
                Compare up to 6 ingested listings with structured insights.
              </SheetDescription>
            </SheetHeader>
            <div className="space-y-4 overflow-y-auto px-4 pb-6">
              <div className="flex flex-wrap gap-2">
                {selectedCompareListings.length === 0 && (
                  <Badge variant="outline">No listings selected</Badge>
                )}
                {selectedCompareListings.map((listing) => (
                  <Badge key={getListingId(listing)} variant="secondary">
                    {listing.title || listing.url || listing.id}
                  </Badge>
                ))}
              </div>
              <div className="flex items-center gap-2">
                <Button
                  onClick={generateComparison}
                  disabled={
                    compareIds.size < 2 ||
                    compareStatus === 'loading' ||
                    (settings.requireMinCompareCoverage && compareCoverageViolations.length > 0)
                  }
                >
                  {compareStatus === 'loading' ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    'Generate comparison'
                  )}
                </Button>
                <Button variant="ghost" onClick={clearCompare} disabled={compareIds.size === 0}>
                  Clear selection
                </Button>
              </div>
              {settings.requireMinCompareCoverage && compareCoverageViolations.length > 0 && (
                <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-200">
                  <p className="font-medium">
                    Comparison blocked by coverage policy ({Math.round(compareMinCoverageRatio * 100)}% minimum).
                  </p>
                  <ul className="mt-2 list-disc space-y-1 pl-4">
                    {compareCoverageViolations.slice(0, 6).map((item) => (
                      <li key={`cov-violation-${getListingId(item.listing) || item.listing?.url}`}>
                        {(item.listing?.title || getListingId(item.listing) || 'Listing')}:{' '}
                        {Math.round(item.coverage * 100)}% ({item.captured}/{item.target})
                      </li>
                    ))}
                  </ul>
                  <div className="mt-3">
                    <Button variant="outline" size="sm" onClick={upgradeCompareReviews}>
                      Fetch full reviews for selected
                    </Button>
                  </div>
                </div>
              )}
              {compareStatus === 'error' && compareError && (
                <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
                  {compareError}
                </div>
              )}
              {compareStatus === 'ready' && compareSummary && (
                <div className="space-y-4">
                  {settings.compareDisclaimer && compareNeedsFull && (
                    <div className="rounded-xl border border-border/60 bg-background/60 p-3 text-xs text-muted-foreground">
                      <p>
                        Comparison uses available review samples (up to {settings.liteReviewCount} per listing).
                        Some listings have more reviews available.
                      </p>
                      <div className="mt-2 flex flex-wrap gap-2">
                        {selectedCompareListings.map((listing) => {
                          const label = formatReviewCoverage(listing)
                          if (!label) return null
                          return (
                            <Badge key={`cov-${getListingId(listing)}`} variant="outline">
                              {label}
                            </Badge>
                          )
                        })}
                      </div>
                      <div className="mt-3">
                        <Button variant="outline" size="sm" onClick={upgradeCompareReviews}>
                          Fetch full reviews for selected
                        </Button>
                      </div>
                    </div>
                  )}
                  <div className="rounded-xl border border-border/60 bg-background/60 p-3 text-sm">
                    <p className="text-xs uppercase text-muted-foreground">Summary</p>
                    <p className="mt-2">{compareSummary.summary}</p>
                  </div>
                  <div className="rounded-xl border border-border/60 bg-background/60 p-3 text-sm">
                    <p className="text-xs uppercase text-muted-foreground">Winner</p>
                    <p className="mt-2 font-semibold">
                      {compareSummary.winner?.listing_id
                        ? compareListingMap.get(String(compareSummary.winner.listing_id))?.title ||
                          compareSummary.winner.listing_id
                        : 'No clear winner'}
                    </p>
                    <p className="mt-2 text-muted-foreground">{compareSummary.winner?.reason}</p>
                  </div>
                  {Array.isArray(compareSummary.tradeoffs) && compareSummary.tradeoffs.length > 0 && (
                    <div className="rounded-xl border border-border/60 bg-background/60 p-3 text-sm">
                      <p className="text-xs uppercase text-muted-foreground">Key tradeoffs</p>
                      <ul className="mt-2 list-disc space-y-1 pl-4">
                        {compareSummary.tradeoffs.map((item, idx) => (
                          <li key={`${item}-${idx}`}>{item}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  <div className="grid gap-3 sm:grid-cols-2">
                    {Array.isArray(compareSummary.sections) &&
                      compareSummary.sections.map((section) => (
                        <div key={section.section} className="rounded-xl border border-border/60 bg-background/60 p-3 text-sm">
                          <div className="flex items-center justify-between">
                            <p className="font-semibold">{section.section}</p>
                            <Badge variant="outline">
                              {section.winner_listing_id
                                ? compareListingMap.get(String(section.winner_listing_id))?.title ||
                                  section.winner_listing_id
                                : 'Tie'}
                            </Badge>
                          </div>
                          <ul className="mt-2 list-disc space-y-1 pl-4 text-muted-foreground">
                            {(section.notes || []).map((note, idx) => (
                              <li key={`${section.section}-${idx}`}>{note}</li>
                            ))}
                          </ul>
                        </div>
                      ))}
                  </div>
                  <div className="space-y-3">
                    {Array.isArray(compareSummary.listing_notes) &&
                      compareSummary.listing_notes.map((item) => (
                        <div key={item.listing_id} className="rounded-xl border border-border/60 bg-background/60 p-3 text-sm">
                          <p className="font-semibold">
                            {item.title ||
                              compareListingMap.get(String(item.listing_id))?.title ||
                              item.listing_id}
                          </p>
                          <div className="mt-2 grid gap-3 sm:grid-cols-2">
                            <div>
                              <p className="text-xs uppercase text-muted-foreground">Pros</p>
                              <ul className="mt-1 list-disc space-y-1 pl-4 text-muted-foreground">
                                {(item.pros || []).map((note, idx) => (
                                  <li key={`${item.listing_id}-pro-${idx}`}>{note}</li>
                                ))}
                              </ul>
                            </div>
                            <div>
                              <p className="text-xs uppercase text-muted-foreground">Cons</p>
                              <ul className="mt-1 list-disc space-y-1 pl-4 text-muted-foreground">
                                {(item.cons || []).map((note, idx) => (
                                  <li key={`${item.listing_id}-con-${idx}`}>{note}</li>
                                ))}
                              </ul>
                            </div>
                          </div>
                          {item.watchouts && item.watchouts.length > 0 && (
                            <div className="mt-2">
                              <p className="text-xs uppercase text-muted-foreground">Watchouts</p>
                              <ul className="mt-1 list-disc space-y-1 pl-4 text-muted-foreground">
                                {item.watchouts.map((note, idx) => (
                                  <li key={`${item.listing_id}-watch-${idx}`}>{note}</li>
                                ))}
                              </ul>
                            </div>
                          )}
                        </div>
                      ))}
                  </div>
                </div>
              )}
            </div>
          </SheetContent>
        </Sheet>
        <Sheet open={detailsOpen} onOpenChange={setDetailsOpen}>
        <SheetContent className="w-full sm:max-w-xl">
          <SheetHeader>
            <SheetTitle>{detailsListing?.title || 'Listing details'}</SheetTitle>
            <SheetDescription>
              {getListingLocation(detailsListing) || 'Unknown location'}
            </SheetDescription>
          </SheetHeader>
          <div className="space-y-4 overflow-y-auto px-4 pb-6">
            {detailsLoading && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" /> Loading listing...
              </div>
            )}
            {!detailsLoading && detailsListing && (
              <>
                <div className="rounded-xl border border-border/60 bg-background/60 p-3">
                  <div className="flex items-start gap-4">
                    <div className="h-20 w-24 overflow-hidden rounded-lg bg-muted">
                      {getPrimaryPhoto(detailsListing) ? (
                        <img
                          src={getPrimaryPhoto(detailsListing)}
                          alt={detailsListing.title || 'Listing'}
                          className="h-full w-full object-cover"
                        />
                      ) : (
                        <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
                          No photo
                        </div>
                      )}
                    </div>
                    <div className="flex-1 space-y-1">
                      <p className="text-sm font-semibold">
                        {detailsListing.property_type || 'Property type'}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {getListingLocation(detailsListing) || 'Unknown location'}
                      </p>
                      <div className="flex flex-wrap gap-2 pt-1">
                        <Badge variant={getCaptureStageBadgeVariant(detailsListing)}>
                          {detailsStageLabel}
                        </Badge>
                        {detailsListing.reviews_summary?.overall_rating && (
                          <Badge variant="secondary">
                            Rating {detailsListing.reviews_summary.overall_rating}
                          </Badge>
                        )}
                        {detailsListing.reviews_summary?.count && (
                          <Badge variant="outline">
                            {detailsListing.reviews_summary.count} reviews
                          </Badge>
                        )}
                        {detailsCoverageLabel && (
                          <Badge variant="outline">{detailsCoverageLabel}</Badge>
                        )}
                      </div>
                    </div>
                    {detailsListing.url && (
                      <a href={detailsListing.url} target="_blank" rel="noreferrer">
                        <Button variant="outline" size="sm">
                          Open listing
                        </Button>
                      </a>
                    )}
                  </div>
                </div>

                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="rounded-xl border border-border/60 bg-background/60 p-3">
                    <p className="text-xs uppercase text-muted-foreground">Host</p>
                    <p className="text-sm font-semibold">{detailsListing.host?.name || 'Unknown host'}</p>
                    <p className="text-xs text-muted-foreground">
                      {detailsListing.host?.superhost ? 'Superhost' : 'Host'}{' '}
                      {detailsListing.host?.rating ? `â€¢ ${detailsListing.host.rating} rating` : ''}
                    </p>
                  </div>
                  <div className="rounded-xl border border-border/60 bg-background/60 p-3">
                    <div className="flex items-center justify-between">
                      <p className="text-xs uppercase text-muted-foreground">Amenities</p>
                      {amenitiesList.length > 8 && (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setAmenitiesExpanded((prev) => !prev)}
                        >
                          {amenitiesExpanded ? 'Collapse' : 'Expand'}
                        </Button>
                      )}
                    </div>
                    {amenitiesList.length === 0 ? (
                      <p className="text-xs text-muted-foreground">No amenities captured yet.</p>
                    ) : (
                      <div className="mt-2 flex flex-wrap gap-2">
                        {(amenitiesExpanded ? amenitiesList : amenitiesList.slice(0, 8)).map((item) => (
                          <Badge key={item} variant="outline">
                            {item}
                          </Badge>
                        ))}
                      </div>
                    )}
                  </div>
                </div>

                <div className="rounded-xl border border-border/60 bg-background/60 p-3">
                  <p className="text-xs uppercase text-muted-foreground">Description</p>
                  <p className="text-sm">
                    {detailsListing.description || 'No description captured.'}
                  </p>
                </div>

                <div className="rounded-xl border border-border/60 bg-background/60 p-3">
                  <p className="text-xs uppercase text-muted-foreground">Capture health</p>
                  <div className="mt-2 flex items-center gap-2">
                    <Badge variant={getCaptureStageBadgeVariant(detailsListing)}>{detailsStageLabel}</Badge>
                    <Badge variant="secondary">{getValidationLabel(detailsListing)}</Badge>
                    {getValidation(detailsListing).quality_score !== undefined && (
                      <Badge variant="outline">
                        Score {getValidation(detailsListing).quality_score}
                      </Badge>
                    )}
                  </div>
                  {detailsStageGuidance && (
                    <p className="mt-2 text-xs text-muted-foreground">{detailsStageGuidance}</p>
                  )}
                  {getValidation(detailsListing).warnings?.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-2">
                      {getValidation(detailsListing).warnings.slice(0, 6).map((warning) => (
                        <Badge key={warning} variant="outline">
                          {warning}
                        </Badge>
                      ))}
                    </div>
                  )}
                  {getValidation(detailsListing).errors?.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-2">
                      {getValidation(detailsListing).errors.slice(0, 4).map((error) => (
                        <Badge key={error} variant="destructive">
                          {error}
                        </Badge>
                      ))}
                    </div>
                  )}
                  {detailsCoverageLabel && (
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      <Badge variant="outline">{detailsCoverageLabel}</Badge>
                      {detailsNeedsFull && (
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => queueFullReviews(detailsListing)}
                          disabled={detailsReviewExpanding}
                        >
                          {detailsReviewExpanding ? (
                            <Loader2 className="mr-2 h-3 w-3 animate-spin" />
                          ) : null}
                          Fetch full reviews
                        </Button>
                      )}
                    </div>
                  )}
                </div>

                <div className="rounded-xl border border-border/60 bg-background/60 p-3">
                  <div className="flex items-center justify-between gap-2">
                    <p className="text-xs uppercase text-muted-foreground">LLM summary</p>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={generateListingSummary}
                      disabled={llmSummaryStatus === 'submitting' || llmSummaryStatus === 'loading'}
                    >
                      {llmSummaryStatus === 'submitting' ? (
                        <Loader2 className="mr-2 h-3 w-3 animate-spin" />
                      ) : null}
                      Generate
                    </Button>
                  </div>
                  {llmSummaryStatus === 'loading' && (
                    <p className="mt-2 text-sm text-muted-foreground">Checking for existing summary...</p>
                  )}
                  {llmSummaryStatus === 'queued' && (
                    <p className="mt-2 text-sm text-muted-foreground">Summary queued. It will appear when ready.</p>
                  )}
                  {llmSummaryError && <p className="mt-2 text-sm text-destructive">{llmSummaryError}</p>}
                  {llmSummary ? (
                    <div className="mt-3 space-y-3 text-sm">
                      {llmSummary.coverage_note && (
                        <div className="rounded-md border border-border/60 bg-background/60 p-2 text-xs text-muted-foreground">
                          {llmSummary.coverage_note}
                        </div>
                      )}
                      <p>{llmSummary.summary}</p>
                      {llmSummary.best_for?.length > 0 && (
                        <div className="flex flex-wrap gap-2">
                          {llmSummary.best_for.map((item, idx) => (
                            <Badge key={`best-${idx}`} variant="secondary">
                              {item}
                            </Badge>
                          ))}
                        </div>
                      )}
                      {llmSummary.highlights?.length > 0 && (
                        <div>
                          <p className="text-xs uppercase text-muted-foreground">Highlights</p>
                          <ul className="mt-1 list-disc space-y-1 pl-4">
                            {llmSummary.highlights.map((item, idx) => (
                              <li key={`hi-${idx}`}>{item}</li>
                            ))}
                          </ul>
                        </div>
                      )}
                      {(llmSummary.pros?.length > 0 || llmSummary.cons?.length > 0) && (
                        <div className="grid gap-3 md:grid-cols-2">
                          {llmSummary.pros?.length > 0 && (
                            <div>
                              <p className="text-xs uppercase text-muted-foreground">Pros</p>
                              <ul className="mt-1 list-disc space-y-1 pl-4">
                                {llmSummary.pros.map((item, idx) => (
                                  <li key={`pro-${idx}`}>{item}</li>
                                ))}
                              </ul>
                            </div>
                          )}
                          {llmSummary.cons?.length > 0 && (
                            <div>
                              <p className="text-xs uppercase text-muted-foreground">Cons</p>
                              <ul className="mt-1 list-disc space-y-1 pl-4">
                                {llmSummary.cons.map((item, idx) => (
                                  <li key={`con-${idx}`}>{item}</li>
                                ))}
                              </ul>
                            </div>
                          )}
                        </div>
                      )}
                      {llmSummary.risks?.length > 0 && (
                        <div>
                          <p className="text-xs uppercase text-muted-foreground">Risks</p>
                          <ul className="mt-1 list-disc space-y-1 pl-4">
                            {llmSummary.risks.map((item, idx) => (
                              <li key={`risk-${idx}`}>{item}</li>
                            ))}
                          </ul>
                        </div>
                      )}
                      {llmSummary.review_themes?.length > 0 && (
                        <div>
                          <p className="text-xs uppercase text-muted-foreground">Review themes</p>
                          <div className="mt-2 space-y-2">
                            {llmSummary.review_themes.map((theme, idx) => (
                              <div key={`theme-${idx}`} className="rounded-md border border-border/50 p-2">
                                <div className="flex items-center justify-between text-xs text-muted-foreground">
                                  <span>{theme.theme}</span>
                                  <span>{theme.sentiment}</span>
                                </div>
                                <p className="mt-1 text-sm">{theme.evidence}</p>
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  ) : (
                    llmSummaryStatus === 'idle' && (
                      <p className="mt-2 text-sm text-muted-foreground">
                        Generate a summary to see pros/cons and review themes.
                      </p>
                    )
                  )}
                </div>
              </>
            )}

            <div className="rounded-xl border border-border/60 bg-background/60 p-3">
              <div className="flex items-center justify-between">
                <p className="text-xs uppercase text-muted-foreground">Recent reviews</p>
                {detailsCoverageLabel ? (
                  <Badge variant="outline">{detailsCoverageLabel}</Badge>
                ) : (
                  detailsListing?.reviews_summary?.count && (
                    <Badge variant="outline">{detailsListing.reviews_summary.count} total</Badge>
                  )
                )}
              </div>
              {detailsReviewsLoading && (
                <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
                  <Loader2 className="h-3 w-3 animate-spin" /> Loading reviews...
                </div>
              )}
              {!detailsReviewsLoading && detailsReviews.length === 0 && (
                <p className="mt-2 text-xs text-muted-foreground">No reviews loaded yet.</p>
              )}
              <div className="mt-3 space-y-3">
                {detailsReviews.map((review) => (
                  <div key={review.id} className="rounded-lg border border-border/60 p-2 text-xs">
                    <div className="flex items-center justify-between">
                      <span className="font-semibold">{review.reviewer?.name || 'Guest'}</span>
                      {review.rating && <Badge variant="secondary">Rating {review.rating}</Badge>}
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground">{review.text || 'No review text'}</p>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </SheetContent>
      </Sheet>

      <div className="w-full px-4 py-6 sm:px-6 lg:px-8">
        <header className="mb-6">
          <p className="text-xs uppercase tracking-[0.3em] text-muted-foreground">Rental Dashboard</p>
          <h1 className="text-3xl font-semibold tracking-tight">Rental Dashboard</h1>
          <p className="text-sm text-muted-foreground">Search runs, listings, and ingest workflows.</p>
        </header>
        <div className="flex flex-col gap-6 lg:flex-row">
        <aside className={`${railWidthClass} shrink-0 rounded-2xl border border-border/60 bg-card/50 p-4`}>
          <div className={`flex items-center ${railCollapsed ? 'justify-center' : 'justify-between'}`}>
            {!railCollapsed && (
              <div>
                <p className="text-lg font-semibold">Search runs</p>
              </div>
            )}
            <Button variant="ghost" size="icon" onClick={() => setRailCollapsed((prev) => !prev)}>
              {railCollapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronRight className="h-4 w-4 rotate-180" />}
            </Button>
          </div>

          {railCollapsed ? (
            <div className="mt-6 flex flex-col items-center gap-3">
              <Button variant="ghost" size="icon" onClick={() => setSearchDialogOpen(true)}>
                <Plus className="h-4 w-4" />
              </Button>
              <Button variant="ghost" size="icon" onClick={() => setUrlDialogOpen(true)}>
                <ExternalLink className="h-4 w-4" />
              </Button>
              <Button variant="ghost" size="icon" onClick={ingestSelected} disabled={selectedIds.size === 0 || ingesting}>
                <CheckCircle2 className="h-4 w-4" />
              </Button>
              <Button variant="ghost" size="icon" onClick={() => setMemoryDialogOpen(true)}>
                <Database className="h-4 w-4" />
              </Button>
              <Button variant="ghost" size="icon" onClick={refreshRuns} disabled={loadingRuns}>
                <RefreshCcw className="h-4 w-4" />
              </Button>
              <Button variant="ghost" size="icon" onClick={() => setSettingsOpen(true)}>
                <SlidersHorizontal className="h-4 w-4" />
              </Button>
              <Button variant="ghost" size="icon" onClick={() => setTheme((prev) => (prev === 'dark' ? 'light' : 'dark'))}>
                {theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
              </Button>
            </div>
          ) : (
            <div className="mt-4 space-y-4">
              <div className="rounded-lg border border-border/60 bg-background/40 p-3">
                <button
                  className="flex w-full items-center justify-between text-left"
                  onClick={() => setActionsOpen((prev) => !prev)}
                >
                  <div>
                    <p className="text-sm font-semibold">Actions</p>
                    <p className="text-xs text-muted-foreground">Run searches and ingest listings.</p>
                  </div>
                  <ChevronRight className={`h-4 w-4 transition ${actionsOpen ? 'rotate-90' : ''}`} />
                </button>
                {actionsOpen && (
                  <div className="mt-3 space-y-3">
                    <div className="flex flex-wrap gap-2">
                      <Button variant="outline" className="w-full justify-start" onClick={() => setSearchDialogOpen(true)}>
                        <Plus className="h-4 w-4" />
                        <span className="ml-2">New search</span>
                      </Button>
                      <Button variant="outline" className="w-full justify-start" onClick={() => setUrlDialogOpen(true)}>
                        Ingest URLs
                      </Button>
                      <Button onClick={ingestSelected} disabled={selectedIds.size === 0 || ingesting} className="w-full">
                        {ingesting ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
                        <span className="ml-2">Ingest selected</span>
                      </Button>
                      <Button variant="outline" className="w-full justify-start" onClick={() => setSettingsOpen(true)}>
                        Settings
                      </Button>
                      <Button variant="outline" className="w-full justify-start" onClick={() => setMemoryDialogOpen(true)}>
                        <Database className="h-4 w-4" />
                        <span className="ml-2">Trip memory</span>
                      </Button>
                    </div>
                    <div className="flex items-center justify-between text-xs text-muted-foreground">
                      <span>
                        {loadingJobs || jobStats.running > 0 || jobStats.queued > 0 ? 'Processing' : 'Idle'}
                      </span>
                      <span>
                        Queued {jobStats.queued} | Running {jobStats.running}
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      <Button variant="ghost" size="sm" onClick={refreshRuns} disabled={loadingRuns}>
                        {loadingRuns ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCcw className="mr-2 h-4 w-4" />}
                        Refresh
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setTheme((prev) => (prev === 'dark' ? 'light' : 'dark'))}
                      >
                        {theme === 'dark' ? <Sun className="mr-2 h-4 w-4" /> : <Moon className="mr-2 h-4 w-4" />}
                        {theme === 'dark' ? 'Light mode' : 'Dark mode'}
                      </Button>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      Ingest selected queues full listing captures (details + reviews) for checked cards.
                    </p>
                  </div>
                )}
              </div>

              <div className="rounded-lg border border-border/60 bg-background/40 p-3">
                <button
                  className="flex w-full items-center justify-between text-left"
                  onClick={() => setIngestedOpen((prev) => !prev)}
                >
                  <div>
                    <p className="text-sm font-semibold">Ingested listings</p>
                    <p className="text-xs text-muted-foreground">Recently ingested detail captures.</p>
                  </div>
                  <ChevronRight className={`h-4 w-4 transition ${ingestedOpen ? 'rotate-90' : ''}`} />
                </button>
                {ingestedOpen && (
                  <ScrollArea className="mt-3 h-[200px] pr-2">
                    <div className="space-y-2 text-sm">
                      {loadingIngested && (
                        <div className="flex items-center gap-2 text-xs text-muted-foreground">
                          <Loader2 className="h-3 w-3 animate-spin" /> Loading ingests...
                        </div>
                      )}
                      {!loadingIngested && ingestedListings.length === 0 && (
                        <div className="rounded-lg border border-dashed p-3 text-xs text-muted-foreground">
                          No ingested listings yet.
                        </div>
                      )}
                    {ingestedListings.map((listing) => {
                      const listingKey = String(getListingId(listing) || listing.url || '')
                      const stageLabel = getCaptureStageLabel(listing)
                      const stageGuidance = getCaptureStageGuidance(listing)
                      const coverageLabel = formatReviewCoverage(listing)
                      const showFullReviews = needsFullReviews(listing)
                      const expanding = listingKey && reviewExpanding.has(listingKey)
                      return (
                        <div key={listing.id || listing.listing_id || listing.url} className="rounded-lg border border-border/60 p-2">
                          <div className="flex flex-col gap-2">
                            <div className="min-w-0">
                              <p className="text-xs font-semibold truncate">
                                {listing.title || listing.url || listing.id || 'Untitled listing'}
                              </p>
                              <p className="text-[11px] text-muted-foreground truncate">
                                {getListingLocation(listing) || 'Unknown location'}
                              </p>
                              <div className="mt-1 flex flex-wrap gap-1">
                                <Badge variant={getCaptureStageBadgeVariant(listing)} className="text-[10px]">
                                  {stageLabel}
                                </Badge>
                                <Badge variant="outline" className="text-[10px]">
                                  {getValidationLabel(listing)}
                                </Badge>
                                {coverageLabel && (
                                  <Badge variant="outline" className="text-[10px]">
                                    {coverageLabel}
                                  </Badge>
                                )}
                              </div>
                            </div>
                            {stageGuidance && (
                              <p className="text-[11px] text-muted-foreground">{stageGuidance}</p>
                            )}
                            <div className="flex items-center gap-2">
                              <Button variant="outline" size="sm" onClick={() => openListingDetails(listing)}>
                                Details
                              </Button>
                              {showFullReviews && (
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  onClick={() => queueFullReviews(listing)}
                                  disabled={expanding}
                                >
                                  {expanding ? <Loader2 className="mr-2 h-3 w-3 animate-spin" /> : null}
                                  Full reviews
                                </Button>
                              )}
                              {listing.url && (
                                <a href={listing.url} target="_blank" rel="noreferrer">
                                  <Button variant="ghost" size="icon">
                                    <ExternalLink className="h-4 w-4" />
                                  </Button>
                                </a>
                              )}
                            </div>
                          </div>
                        </div>
                      )
                    })}
                    </div>
                  </ScrollArea>
                )}
              </div>

              <div className="rounded-lg border border-border/60 bg-background/40 p-3">
                <button
                  className="flex w-full items-center justify-between text-left"
                  onClick={() => setJobsOpen((prev) => !prev)}
                >
                  <div>
                    <p className="text-sm font-semibold">Job history</p>
                    <p className="text-xs text-muted-foreground">Recent background jobs.</p>
                  </div>
                  <ChevronRight className={`h-4 w-4 transition ${jobsOpen ? 'rotate-90' : ''}`} />
                </button>
                {jobsOpen && (
                  <ScrollArea className="mt-3 h-[200px] pr-2">
                    <div className="space-y-2 text-xs">
                      {jobHistory.length === 0 && (
                        <div className="rounded-lg border border-dashed p-3 text-xs text-muted-foreground">
                          No jobs recorded yet.
                        </div>
                      )}
                      {jobHistory.map((job) => (
                        <div key={job.job_id} className="flex items-center justify-between gap-2 rounded-lg border border-border/60 p-2">
                          <div>
                            <p className="font-semibold">{formatJobLabel(job)}</p>
                            <p className="text-[11px] text-muted-foreground">{formatTimestamp(job.updated_at)}</p>
                          </div>
                          <Badge variant={job.status === 'failed' ? 'destructive' : job.status === 'complete' ? 'secondary' : 'outline'}>
                            {job.status}
                          </Badge>
                        </div>
                      ))}
                    </div>
                  </ScrollArea>
                )}
              </div>

              <div className="rounded-lg border border-border/60 bg-background/40 p-3">
                <button
                  className="flex w-full items-center justify-between text-left"
                  onClick={() => setRunsOpen((prev) => !prev)}
                >
                  <div>
                    <p className="text-sm font-semibold">Search Runs</p>
                    <p className="text-xs text-muted-foreground">Pick a run to inspect captured listings.</p>
                  </div>
                  <ChevronRight className={`h-4 w-4 transition ${runsOpen ? 'rotate-90' : ''}`} />
                </button>
                {runsOpen && (
                  <ScrollArea className="mt-3 h-[320px] pr-2">
                    <div className="space-y-2">
                      {runs.length === 0 && (
                        <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
                          No runs yet. Trigger a search to populate this list.
                        </div>
                      )}
                      {runs.map((run) => {
                        const isActive = run.run_id === selectedRunId
                        return (
                          <button
                            key={run.run_id}
                            onClick={() => setSelectedRunId(run.run_id)}
                            className={`w-full rounded-lg border px-3 py-3 text-left transition ${
                              isActive
                                ? 'border-primary/40 bg-primary/10'
                                : 'border-border hover:border-primary/30 hover:bg-muted/50'
                            }`}
                          >
                            <div className="flex items-center justify-between">
                              <span className="text-sm font-medium">{run.params?.location || 'Unknown location'}</span>
                              <ChevronRight className="h-4 w-4 text-muted-foreground" />
                            </div>
                            <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
                              <CalendarDays className="h-3 w-3" />
                              <span>{formatTimestamp(run.created_at)}</span>
                            </div>
                            <div className="mt-2 flex flex-wrap gap-2">
                              <Badge variant="secondary">Listings: {run.result?.listing_count ?? 'n/a'}</Badge>
                              <Badge variant="outline">Responses: {run.result?.response_count ?? 'n/a'}</Badge>
                            </div>
                          </button>
                        )
                      })}
                    </div>
                  </ScrollArea>
                )}
              </div>

              <div className="rounded-lg border border-border/60 bg-background/40 p-3">
                <button
                  className="flex w-full items-center justify-between text-left"
                  onClick={() => setSummaryOpen((prev) => !prev)}
                >
                  <div>
                    <p className="text-sm font-semibold">Run Summary</p>
                    <p className="text-xs text-muted-foreground">Health check for the selected run.</p>
                  </div>
                  <ChevronRight className={`h-4 w-4 transition ${summaryOpen ? 'rotate-90' : ''}`} />
                </button>
                {summaryOpen && (
                  <div className="mt-3 grid gap-3 text-sm">
                    <div className="flex items-center justify-between">
                      <span className="text-muted-foreground">Total listings</span>
                      <span className="font-semibold">{summary?.total_listings ?? 'n/a'}</span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-muted-foreground">Quality score</span>
                      <span className="font-semibold">{summary?.avg_quality_score ?? 'n/a'}</span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-muted-foreground">Runs with errors</span>
                      <span className="font-semibold">{summary?.with_errors ?? 'n/a'}</span>
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {summary ? summarizeMissing(summary.missing_fields) : 'n/a'}
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}
        </aside>

        <main className="min-w-0 flex-1 space-y-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="inline-flex rounded-full border border-border/60 bg-background/60 p-1 text-sm">
              <Button
                variant={viewMode === 'search' ? 'secondary' : 'ghost'}
                size="sm"
                onClick={() => setViewMode('search')}
              >
                Search results
              </Button>
              <Button
                variant={viewMode === 'ingested' ? 'secondary' : 'ghost'}
                size="sm"
                onClick={() => setViewMode('ingested')}
              >
                Ingested library
              </Button>
            </div>
            {viewMode === 'ingested' && compareIds.size >= 2 && (
              <div className="flex flex-wrap items-center gap-2 rounded-full border border-border/60 bg-background/60 px-3 py-2 text-xs">
                <span>{compareIds.size} selected for comparison</span>
                <Button size="sm" onClick={() => setCompareOpen(true)}>
                  Compare
                </Button>
                <Button variant="ghost" size="sm" onClick={clearCompare}>
                  Clear
                </Button>
              </div>
            )}
          </div>

          {viewMode === 'search' ? (
            <Card>
              <CardHeader className="pb-3">
                <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                  <div>
                    <CardTitle className="text-lg">Listings</CardTitle>
                    <CardDescription>Filter, select, and ingest listings.</CardDescription>
                  </div>
                  <div className="flex flex-1 items-center gap-2 md:max-w-md">
                    <div className="relative flex-1">
                      <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                      <Input
                        value={searchTerm}
                        onChange={(event) => setSearchTerm(event.target.value)}
                        placeholder="Search title, location, type"
                        className="pl-9"
                      />
                    </div>
                    <Button variant="outline" onClick={selectAll} disabled={filteredListings.length === 0}>
                      Select all
                    </Button>
                    <Button variant="ghost" onClick={clearSelection} disabled={selectedIds.size === 0}>
                      Clear
                    </Button>
                  </div>
                </div>
              </CardHeader>
              <Separator />
              <CardContent className="pt-4">
                {loadingListings && (
                  <div className="flex items-center gap-2 text-sm text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin" /> Loading listings...
                  </div>
                )}
                {!loadingListings && filteredListings.length === 0 && (
                  <div className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">
                    No listings found for this run.
                  </div>
                )}
                <div className="grid gap-4 md:grid-cols-2">
                  {filteredListings.map((listing) => {
                    const checked = selectedIds.has(listing.id)
                    const listingId = listing.id ? String(listing.id) : null
                    const isIngested = listingId ? ingestedIdSet.has(listingId) : false
                    const priceLabel = getUsdPriceLabel(listing, settings.priceDisplay)
                    const rating = getListingRating(listing)
                    const { lat, lng } = getListingCoordinates(listing)
                    return (
                      <Card key={listing.id} className="overflow-hidden">
                        <div className="relative h-40 w-full overflow-hidden bg-muted">
                          {listing.image ? (
                            <img
                              src={listing.image}
                              alt={listing.title || 'Listing'}
                              className="h-full w-full object-cover"
                            />
                          ) : (
                            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                              No image
                            </div>
                          )}
                          <div className="absolute left-3 top-3 rounded-full bg-background/80 p-2">
                            <Checkbox checked={checked} onCheckedChange={() => toggleSelection(listing.id)} />
                          </div>
                        </div>
                        <CardContent className="space-y-3 pt-4">
                          <div className="flex items-center justify-between">
                            <div>
                              <p className="text-sm font-semibold">{listing.title || 'Untitled listing'}</p>
                              <p className="text-xs text-muted-foreground">
                                {listing.location || 'Unknown location'} - {listing.property_type || 'Unknown type'}
                              </p>
                            </div>
                            <div className="flex items-center gap-2">
                              {isIngested && (
                                <Button
                                  variant="outline"
                                  size="sm"
                                  onClick={() => openListingDetails({ id: listingId })}
                                >
                                  Details
                                </Button>
                              )}
                              {listing.url && (
                                <a href={listing.url} target="_blank" rel="noreferrer">
                                  <Button variant="ghost" size="icon">
                                    <ExternalLink className="h-4 w-4" />
                                  </Button>
                                </a>
                              )}
                            </div>
                          </div>
                          <div className="flex flex-wrap gap-2">
                            <Badge variant="secondary">{priceLabel}</Badge>
                            {rating !== null && (
                              <Badge variant="outline">Rating {rating}</Badge>
                            )}
                            {isIngested && <Badge variant="outline">Ingested</Badge>}
                          </div>
                          <div className="text-xs text-muted-foreground">
                            Lat {lat ?? 'n/a'} - Lng {lng ?? 'n/a'}
                          </div>
                        </CardContent>
                      </Card>
                    )
                  })}
                </div>
              </CardContent>
            </Card>
          ) : (
            <Card>
              <CardHeader className="pb-3">
                <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                  <div>
                    <CardTitle className="text-lg">Ingested library</CardTitle>
                    <CardDescription>Search, compare, and review ingested listings.</CardDescription>
                  </div>
                  <div className="flex flex-1 flex-wrap items-center gap-2 md:max-w-xl">
                    <div className="relative flex-1">
                      <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                      <Input
                        value={ingestedSearchTerm}
                        onChange={(event) => setIngestedSearchTerm(event.target.value)}
                        placeholder="Search ingested listings"
                        className="pl-9"
                      />
                    </div>
                    <select
                      className="h-9 rounded-md border border-border bg-background px-3 text-sm"
                      value={ingestedSort}
                      onChange={(event) => setIngestedSort(event.target.value)}
                    >
                      <option value="newest">Newest</option>
                      <option value="oldest">Oldest</option>
                      <option value="rating">Rating</option>
                      <option value="price_low">Price: low to high</option>
                      <option value="price_high">Price: high to low</option>
                    </select>
                  </div>
                </div>
              </CardHeader>
              <Separator />
              <CardContent className="pt-4">
                {loadingIngested && (
                  <div className="flex items-center gap-2 text-sm text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin" /> Loading ingested listings...
                  </div>
                )}
                {!loadingIngested && sortedIngestedListings.length === 0 && (
                  <div className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">
                    No ingested listings found.
                  </div>
                )}
                <div className="grid gap-4 md:grid-cols-2">
                  {sortedIngestedListings.map((listing) => {
                    const listingId = getListingId(listing)
                    const listingKey = String(listingId || listing.url || '')
                    const stageLabel = getCaptureStageLabel(listing)
                    const stageGuidance = getCaptureStageGuidance(listing)
                    const compareChecked = listingId ? compareIds.has(String(listingId)) : false
                    const coverageLabel = formatReviewCoverage(listing)
                    const showFullReviews = needsFullReviews(listing)
                    const expanding = listingKey && reviewExpanding.has(listingKey)
                    const priceLabel = getUsdPriceLabel(listing, settings.priceDisplay)
                    const rating = getListingRating(listing)
                    const { lat, lng } = getListingCoordinates(listing)
                    return (
                      <Card key={listingId || listing.url} className="overflow-hidden">
                        <div className="relative h-40 w-full overflow-hidden bg-muted">
                          {getPrimaryPhoto(listing) ? (
                            <img
                              src={getPrimaryPhoto(listing)}
                              alt={listing.title || 'Listing'}
                              className="h-full w-full object-cover"
                            />
                          ) : (
                            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                              No image
                            </div>
                          )}
                          <div className="absolute left-3 top-3 rounded-full bg-background/80 p-2">
                            <Checkbox
                              checked={compareChecked}
                              onCheckedChange={() => listingId && toggleCompareSelection(String(listingId))}
                            />
                          </div>
                        </div>
                        <CardContent className="space-y-3 pt-4">
                          <div className="flex items-center justify-between">
                            <div>
                              <p className="text-sm font-semibold">{listing.title || listing.url || 'Untitled listing'}</p>
                              <p className="text-xs text-muted-foreground">
                                {getListingLocation(listing) || 'Unknown location'} - {listing.property_type || 'Unknown type'}
                              </p>
                            </div>
                            <div className="flex items-center gap-2">
                              <Button
                                variant="outline"
                                size="sm"
                                onClick={() => openListingDetails(listing)}
                              >
                                Details
                              </Button>
                              {showFullReviews && (
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  onClick={() => queueFullReviews(listing)}
                                  disabled={expanding}
                                >
                                  {expanding ? <Loader2 className="mr-2 h-3 w-3 animate-spin" /> : null}
                                  Full reviews
                                </Button>
                              )}
                              {listing.url && (
                                <a href={listing.url} target="_blank" rel="noreferrer">
                                  <Button variant="ghost" size="icon">
                                    <ExternalLink className="h-4 w-4" />
                                  </Button>
                                </a>
                              )}
                            </div>
                          </div>
                          <div className="flex flex-wrap gap-2">
                            <Badge variant="secondary">{priceLabel}</Badge>
                            <Badge variant={getCaptureStageBadgeVariant(listing)}>{stageLabel}</Badge>
                            {rating !== null && (
                              <Badge variant="outline">Rating {rating}</Badge>
                            )}
                            <Badge variant="outline">{getValidationLabel(listing)}</Badge>
                            {coverageLabel && <Badge variant="outline">{coverageLabel}</Badge>}
                          </div>
                          {stageGuidance && (
                            <p className="text-xs text-muted-foreground">{stageGuidance}</p>
                          )}
                          <div className="text-xs text-muted-foreground">
                            Lat {lat ?? 'n/a'} - Lng {lng ?? 'n/a'}
                          </div>
                        </CardContent>
                      </Card>
                    )
                  })}
                </div>
              </CardContent>
            </Card>
          )}
      </main>
      {enableAgentChat ? <AgentChat sessionId="rental-dashboard" /> : null}
        </div>
      </div>
    </div>
  )
}
