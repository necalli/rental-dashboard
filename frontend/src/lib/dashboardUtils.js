const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:5002'
const MEMORY_USER_ID = import.meta.env.VITE_RAG_USER_ID || 'rental-dashboard'
const enableAgentChat = String(import.meta.env.VITE_ENABLE_AGENT_CHAT || 'false').toLowerCase() === 'true'
const SETTINGS_KEY = 'rental-settings'
const COMPARE_DRAWER_KEY = 'rental-compare-drawer-width'
const suggestDebounceMs = 400
const defaultSettings = {
  reviewMode: 'lite',
  liteCaptureStrategy: 'adaptive',
  liteReviewCount: 24,
  bulkIngestStrategy: 'review_sample',
  bulkDisableLiteRetry: false,
  compareDisclaimer: true,
  compareUseMemory: false,
  compareMemoryFocus: '',
  compareMemoryLimit: 6,
  requireMinCompareCoverage: false,
  minCompareCoveragePercent: 50,
  compareMax: 6,
  priceDisplay: 'total',
  captureTimeoutMs: 180000,
  reviewPaginationPasses: 8,
  llmModelOverride: '',
}
const clampInteger = (value, min, max, fallback = null) => {
  const parsed = Number.parseInt(value, 10)
  if (Number.isNaN(parsed)) return fallback
  return Math.min(Math.max(parsed, min), max)
}
const buildCaptureOverrides = (settings, { includeReviews = true } = {}) => {
  const payload = {}
  const captureTimeoutMs = clampInteger(settings?.captureTimeoutMs, 10000, 600000, null)
  if (captureTimeoutMs !== null) {
    payload.capture_timeout_ms = captureTimeoutMs
  }
  if (includeReviews) {
    const reviewPaginationPasses = clampInteger(settings?.reviewPaginationPasses, 1, 24, null)
    if (reviewPaginationPasses !== null) {
      payload.review_pagination_passes = reviewPaginationPasses
    }
    const liteCaptureStrategy = settings?.liteCaptureStrategy === 'normal' ? 'normal' : 'adaptive'
    payload.lite_capture_strategy = liteCaptureStrategy
  }
  return payload
}

const getStoredTheme = () => {
  if (typeof window === 'undefined') return 'dark'
  const stored = window.localStorage.getItem('rental-theme')
  if (stored === 'dark' || stored === 'light') return stored
  return 'dark'
}

const formatTimestamp = (value) => {
  if (!value) return 'Unknown'
  const parsed = new Date(value * 1000 || value)
  if (Number.isNaN(parsed.getTime())) return 'Unknown'
  return parsed.toLocaleString()
}
const formatMs = (value) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return 'n/a'
  const numeric = Number(value)
  return `${Math.round(numeric)} ms`
}

const summarizeMissing = (missing = {}) => {
  const entries = Object.entries(missing)
  if (!entries.length) return 'No missing fields'
  return (
    entries
      .filter(([, count]) => Number(count) > 0)
      .map(([key, count]) => `${key}: ${count}`)
      .join(' | ') || 'No missing fields'
  )
}

const safeNumber = (value) => {
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : null
  }
  if (typeof value === 'string') {
    const normalized = value.trim().replace(/,/g, '')
    if (!normalized) return null
    const direct = Number(normalized)
    if (Number.isFinite(direct)) return direct
    const extracted = normalized.match(/-?\d+(?:\.\d+)?/)
    if (!extracted) return null
    const parsed = Number(extracted[0])
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}
const parsePriceValue = (price) => {
  if (!price) return null
  const numeric = String(price).replace(/[^0-9.]/g, '')
  const value = Number.parseFloat(numeric)
  return Number.isNaN(value) ? null : value
}
const formatUsd = (value) => {
  if (value === null || value === undefined || Number.isNaN(value)) return null
  const fractionDigits = Number.isInteger(value) ? 0 : 2
  try {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      maximumFractionDigits: fractionDigits,
    }).format(value)
  } catch {
    const fixed = Number.isInteger(value) ? String(value) : value.toFixed(2)
    return `$${fixed}`
  }
}
const getPricing = (listing) =>
  listing && typeof listing.pricing === 'object' ? listing.pricing : {}
const getUsdPriceValue = (listing, preference = 'total') => {
  const pricing = getPricing(listing)
  const total = safeNumber(pricing.price_total_usd ?? listing?.price_usd_total ?? listing?.price_usd)
  const nightly = safeNumber(pricing.price_nightly_usd ?? listing?.price_usd_nightly)
  let value = null
  let mode = preference
  if (preference === 'nightly') {
    value = nightly ?? total ?? null
    if (value !== null && nightly === null) {
      mode = 'total'
    }
  } else {
    value = total ?? nightly ?? null
    if (value !== null && total === null) {
      mode = 'nightly'
    }
  }
  return { value, mode }
}
const getUsdPriceLabel = (listing, preference = 'total') => {
  const { value, mode } = getUsdPriceValue(listing, preference)
  if (value === null) return 'USD n/a'
  const formatted = formatUsd(value)
  if (!formatted) return 'USD n/a'
  if (mode === 'nightly') {
    return `${formatted}/night`
  }
  return `${formatted} total`
}
const getListingId = (listing) => listing?.id || listing?.listing_id
const extractListingIdFromUrl = (url) => {
  if (!url) return null
  const match = String(url).match(/\/rooms\/([^/?#]+)/)
  return match ? match[1] : null
}
const decodeDragText = (value) =>
  String(value || '')
    .replace(/&amp;/g, '&')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
const normalizeAirbnbUrl = (value) => {
  const raw = decodeDragText(value).trim().replace(/[),.;]+$/g, '')
  if (!raw) return null
  try {
    const parsed = new URL(raw, 'https://www.airbnb.com')
    const host = parsed.hostname.toLowerCase()
    if (host !== 'airbnb.com' && !host.endsWith('.airbnb.com')) return null
    parsed.hash = ''
    return parsed.toString()
  } catch {
    return null
  }
}
const isAirbnbListingUrl = (value) => {
  const normalized = normalizeAirbnbUrl(value)
  if (!normalized) return false
  try {
    return new URL(normalized).pathname.toLowerCase().startsWith('/rooms/')
  } catch {
    return false
  }
}
const isAirbnbSearchUrl = (value) => {
  const normalized = normalizeAirbnbUrl(value)
  if (!normalized) return false
  try {
    const path = new URL(normalized).pathname.toLowerCase()
    return path === '/s' || path.startsWith('/s/')
  } catch {
    return false
  }
}
const extractUrlsFromText = (value) => {
  const text = decodeDragText(value)
  if (!text) return []
  const urls = []
  const absoluteMatches = text.match(/https?:\/\/[^\s"'<>]+/gi) || []
  urls.push(...absoluteMatches)
  const relativeMatches = text.match(/(?:href=["'])?(\/rooms\/[^\s"'<>]+)/gi) || []
  relativeMatches.forEach((match) => {
    const cleaned = match.replace(/^href=["']?/i, '')
    urls.push(cleaned)
  })
  const searchRelativeMatches = text.match(/(?:href=["'])?(\/s\/[^\s"'<>]+)/gi) || []
  searchRelativeMatches.forEach((match) => {
    const cleaned = match.replace(/^href=["']?/i, '')
    urls.push(cleaned)
  })
  return urls
}
const extractAirbnbListingUrlsFromDragPayload = (payload = {}) => {
  const values = [payload.uriList, payload.plainText, payload.html].filter(Boolean)
  const candidates = values.flatMap((value) => extractUrlsFromText(value))
  const output = []
  const seen = new Set()
  candidates.forEach((candidate) => {
    const normalized = normalizeAirbnbUrl(candidate)
    if (!normalized || !isAirbnbListingUrl(normalized) || seen.has(normalized)) return
    seen.add(normalized)
    output.push(normalized)
  })
  return output
}
const dragPayloadHasAirbnbSearchUrl = (payload = {}) => {
  const values = [payload.uriList, payload.plainText, payload.html].filter(Boolean)
  return values.flatMap((value) => extractUrlsFromText(value)).some((candidate) => isAirbnbSearchUrl(candidate))
}
const getListingUrl = (listing) => {
  if (!listing) return null
  if (listing.url) return listing.url
  const listingId = getListingId(listing)
  return listingId ? `https://www.airbnb.com/rooms/${listingId}` : null
}
const getListingLocation = (listing) => {
  if (!listing) return null
  if (typeof listing.location === 'string') return listing.location
  if (listing.location?.name) return listing.location.name
  return null
}
const getListingRating = (listing) =>
  safeNumber(listing?.rating) ??
  safeNumber(listing?.reviews_summary?.overall_rating) ??
  safeNumber(listing?.reviews_summary?.rating) ??
  null
const getListingCoordinates = (listing) => {
  const lat =
    safeNumber(listing?.lat) ??
    safeNumber(listing?.location?.lat) ??
    safeNumber(listing?.location?.details?.lat) ??
    safeNumber(listing?.location?.coordinate?.latitude) ??
    safeNumber(listing?.location?.coordinate?.lat) ??
    null
  const lng =
    safeNumber(listing?.lng) ??
    safeNumber(listing?.location?.lng) ??
    safeNumber(listing?.location?.details?.lng) ??
    safeNumber(listing?.location?.coordinate?.longitude) ??
    safeNumber(listing?.location?.coordinate?.lng) ??
    null
  return { lat, lng }
}
const getReviewCoverage = (listing) => {
  if (!listing) return { captured: null, total: null, mode: null, coverage: null }
  const captured =
    safeNumber(listing.reviews_captured_count) ??
    safeNumber(listing.reviews_count)
  const total =
    safeNumber(listing.reviews_total_count) ??
    safeNumber(listing.reviews_summary?.count) ??
    safeNumber(listing.host?.review_count) ??
    safeNumber(listing.review_count)
  const mode = listing.review_mode || null
  const coverage =
    safeNumber(listing.review_coverage) ??
    (captured && total ? Math.min(captured / total, 1) : null)
  return { captured, total, mode, coverage }
}
const hasReviewCaptureGap = (listing) => {
  const { captured, total, mode } = getReviewCoverage(listing)
  return (mode === 'lite' || mode === 'full') && Number(total || 0) > 0 && Number(captured || 0) <= 0
}
const getComparisonCoverage = (listing, reviewLimit = 24) => {
  const limit = clampInteger(reviewLimit, 1, 50, 24) || 24
  const { captured, total } = getReviewCoverage(listing)
  const capturedValue = captured !== null && captured !== undefined ? Number(captured) : 0
  const totalValue = total !== null && total !== undefined ? Number(total) : null
  const target = totalValue && totalValue > 0 ? Math.max(1, Math.min(limit, totalValue)) : limit
  const coverage = target > 0 ? Math.min(capturedValue / target, 1) : 0
  return {
    captured: capturedValue,
    total: totalValue,
    target,
    coverage,
  }
}
const getCaptureStage = (listing) => {
  const stage = String(listing?.capture_stage || '').trim()
  if (stage === 'reviews_full_ready' || stage === 'reviews_lite_ready' || stage === 'summary_ready') {
    return stage
  }
  const stages = listing?.capture_stages && typeof listing.capture_stages === 'object' ? listing.capture_stages : {}
  let summaryReady = Boolean(stages.summary_ready)
  let liteReady = Boolean(stages.reviews_lite_ready)
  let fullReady = Boolean(stages.reviews_full_ready)
  const hasSummaryFields = Boolean(
    listing?.title ||
      listing?.description ||
      listing?.property_type ||
      listing?.pricing?.price_total ||
      listing?.pricing?.price_nightly ||
      listing?.pricing?.price_display ||
      (Array.isArray(listing?.photos) && listing.photos.length > 0)
  )
  const { captured, total, mode } = getReviewCoverage(listing)
  if (hasSummaryFields) summaryReady = true
  if ((captured || 0) > 0 && (mode === 'lite' || mode === 'full')) liteReady = true
  if ((captured || 0) > 0 && mode === 'full' && (!total || captured >= total)) fullReady = true
  if (fullReady) return 'reviews_full_ready'
  if (liteReady) return 'reviews_lite_ready'
  if (summaryReady) return 'summary_ready'
  return 'capture_pending'
}
const getCaptureStageLabel = (listing) => {
  const stage = getCaptureStage(listing)
  if (stage === 'reviews_full_ready') return 'Full reviews ready'
  if (stage === 'reviews_lite_ready') return 'Lite reviews ready'
  if (stage === 'summary_ready') return 'Summary ready'
  return 'Capture pending'
}
const getCaptureStageBadgeVariant = (listing) => {
  const stage = getCaptureStage(listing)
  if (stage === 'reviews_full_ready') return 'secondary'
  return 'outline'
}
const formatReviewCoverage = (listing) => {
  const { captured, total, mode } = getReviewCoverage(listing)
  const capturedValue = captured !== null && captured !== undefined ? Number(captured) : null
  const totalValue = total !== null && total !== undefined ? Number(total) : null
  if ((mode === 'lite' || mode === 'full') && totalValue && !capturedValue) {
    return `Reviews 0/${totalValue} (${mode})`
  }
  if (!capturedValue && !totalValue) return mode ? `Reviews (${mode})` : null
  let label = 'Reviews'
  if (capturedValue && totalValue) {
    label = `Reviews ${capturedValue}/${totalValue}`
  } else if (totalValue) {
    label = mode ? `Reviews 0/${totalValue}` : `Reviews ${totalValue} total`
  } else if (capturedValue) {
    label = `Reviews ${capturedValue}`
  }
  if (mode) {
    label = `${label} (${mode})`
  }
  return label
}
const needsFullReviews = (listing) => {
  const { captured, total, mode } = getReviewCoverage(listing)
  if (mode === 'full') return false
  if (!total) return true
  if (mode === 'full' && captured && captured >= total) return false
  if (captured && captured >= total) return false
  return true
}
const getCaptureStageGuidance = (listing) => {
  const stage = getCaptureStage(listing)
  if (stage === 'reviews_full_ready') return 'Full-review capture complete.'
  if (stage === 'reviews_lite_ready') {
    if (needsFullReviews(listing)) {
      return 'Run Full reviews for higher-confidence comparisons.'
    }
    return 'Ready for comparisons with captured review sample.'
  }
  if (stage === 'summary_ready') {
    return 'Summary data is ready. Capture reviews to enrich insights.'
  }
  return 'Capture is still in progress.'
}
const getValidation = (listing) => listing?.validation || {}
const getValidationLabel = (listing) => {
  const validation = getValidation(listing)
  const errors = validation.errors || []
  const warnings = validation.warnings || []
  if (hasReviewCaptureGap(listing)) return 'Partial'
  if (errors.length > 0) return 'Failed'
  if (warnings.length > 0) return 'Partial'
  return 'Complete'
}
const getMetricValue = (entry, key) => {
  if (!entry || typeof entry !== 'object') return null
  const metrics = entry.metrics && typeof entry.metrics === 'object' ? entry.metrics : {}
  if (key in metrics) return safeNumber(metrics[key])
  const capture = metrics.capture_timings && typeof metrics.capture_timings === 'object' ? metrics.capture_timings : {}
  if (key in capture) return safeNumber(capture[key])
  return null
}
const getAmenityLabel = (amenity) => {
  if (!amenity) return null
  if (typeof amenity === 'string') return amenity
  return amenity.name || amenity.title || amenity.label || amenity.text || null
}
const getAmenitiesList = (listing) => {
  const groups = Array.isArray(listing?.amenities) ? listing.amenities : []
  const all = []
  groups.forEach((group) => {
    const groupName = group?.group || group?.name || null
    const items = Array.isArray(group?.items) ? group.items : []
    items.forEach((item) => {
      const label = getAmenityLabel(item)
      if (label) {
        all.push(groupName ? `${groupName}: ${label}` : label)
      }
    })
  })
  return all
}
const getPrimaryPhoto = (listing) => {
  const photo = listing?.photos?.[0]
  if (!photo) return null
  if (typeof photo === 'string') return photo
  return (
    photo.url ||
    photo.originalPicture ||
    photo.picture ||
    photo.large ||
    photo.xlPicture ||
    null
  )
}
const getPhotoUrl = (photo) => {
  if (!photo) return null
  if (typeof photo === 'string') return photo
  return (
    photo.url ||
    photo.originalPicture ||
    photo.picture ||
    photo.large ||
    photo.xlPicture ||
    photo.thumbnailUrl ||
    null
  )
}
const cleanPhotoText = (value) => {
  if (value === null || value === undefined) return null
  if (typeof value === 'object') {
    if (Array.isArray(value)) {
      return value.map(cleanPhotoText).filter(Boolean).join(' ') || null
    }
    return (
      cleanPhotoText(value.text) ||
      cleanPhotoText(value.title) ||
      cleanPhotoText(value.caption) ||
      cleanPhotoText(value.localizedText) ||
      cleanPhotoText(value.value)
    )
  }
  const text = String(value).trim()
  return text || null
}
const getPhotoArea = (photo) => {
  if (!photo || typeof photo === 'string') return 'Unlabeled'
  return cleanPhotoText(photo.room_or_area || photo.roomType || photo.roomTitle) || 'Unlabeled'
}
const getPhotoCaption = (photo) => {
  if (!photo || typeof photo === 'string') return null
  return cleanPhotoText(photo.localized_caption || photo.localizedCaption || photo.caption || photo.title)
}
const getPhotoAreaSummary = (listing) => {
  const photos = Array.isArray(listing?.photos) ? listing.photos : []
  const counts = new Map()
  photos.forEach((photo) => {
    if (!getPhotoUrl(photo)) return
    const area = getPhotoArea(photo)
    counts.set(area, (counts.get(area) || 0) + 1)
  })
  return Array.from(counts.entries())
    .map(([area, count]) => ({ area, count }))
    .sort((a, b) => {
      if (a.area === 'Unlabeled') return 1
      if (b.area === 'Unlabeled') return -1
      return b.count - a.count || a.area.localeCompare(b.area)
    })
}
const getRepresentativePhotos = (listing) => {
  const explicit =
    listing?.representative_photos && typeof listing.representative_photos === 'object'
      ? listing.representative_photos
      : {}
  const representatives = []
  const seenAreas = new Set()
  Object.entries(explicit).forEach(([area, photo]) => {
    const url = getPhotoUrl(photo)
    if (!area || !url || seenAreas.has(area)) return
    seenAreas.add(area)
    representatives.push({ area, url, caption: getPhotoCaption(photo) })
  })

  const photos = Array.isArray(listing?.photos) ? listing.photos : []
  photos.forEach((photo) => {
    const area = getPhotoArea(photo)
    const url = getPhotoUrl(photo)
    if (!area || !url || seenAreas.has(area)) return
    seenAreas.add(area)
    representatives.push({ area, url, caption: getPhotoCaption(photo) })
  })

  return representatives.filter((item) => item.area !== 'Unlabeled').slice(0, 8)
}
const getPreferenceAlignment = (listing) =>
  listing?.preference_alignment && typeof listing.preference_alignment === 'object'
    ? listing.preference_alignment
    : null
const getPreferenceScore = (listing) => safeNumber(getPreferenceAlignment(listing)?.score) ?? 0
const hasPreferenceAlignment = (listing) => {
  const alignment = getPreferenceAlignment(listing)
  return Boolean(alignment && Number(alignment.requested_count || 0) > 0)
}
const getPreferenceAlignmentLabel = (listing) => {
  const alignment = getPreferenceAlignment(listing)
  if (!alignment || !alignment.requested_count) return null
  return `Preference ${alignment.matched_count || 0}/${alignment.requested_count}`
}
const getDateContext = (listing) =>
  listing?.date_context && typeof listing.date_context === 'object' ? listing.date_context : {}
const getDateMatchType = (listing) => getDateContext(listing).date_match_type || null
const getListingDatePair = (listing) => {
  const dates = getDateContext(listing).listing_dates
  return dates && typeof dates === 'object' ? dates : {}
}
const getRequestedDatePair = (listing) => {
  const dates = getDateContext(listing).requested_dates
  return dates && typeof dates === 'object' ? dates : {}
}
const formatDateRange = (dates) => {
  if (!dates?.check_in || !dates?.check_out) return null
  return `${dates.check_in} to ${dates.check_out}`
}
const getDateContextLabel = (listing) => {
  const matchType = getDateMatchType(listing)
  const listingDates = getListingDatePair(listing)
  if (matchType === 'flexible_alternate' || matchType === 'alternate') {
    const range = formatDateRange(listingDates)
    return range ? `Alternate dates: ${range}` : 'Alternate dates'
  }
  if (matchType === 'exact') return 'Exact dates'
  if (matchType === 'unknown_flexible') return 'Flexible dates unknown'
  return null
}
const getDateContextVariant = (listing) => {
  const matchType = getDateMatchType(listing)
  if (matchType === 'flexible_alternate' || matchType === 'alternate') return 'secondary'
  if (matchType === 'unknown_flexible') return 'outline'
  return 'outline'
}
const formatJobLabel = (job) => {
  if (!job) return 'Job'
  if (job.job_type === 'listing_ingest') {
    return job.payload?.url ? 'Listing ingest' : 'Listing ingest'
  }
  if (job.job_type === 'listing_enrich') return 'Listing summary'
  if (job.job_type === 'listing_compare') return 'Listing comparison'
  if (job.job_type === 'search') return 'Search'
  return job.job_type || 'Job'
}

const requestJson = async (path, options) => {
  const res = await fetch(`${API_BASE}${path}`, options)
  if (!res.ok) {
    let errorPayload = null
    let message = ''
    try {
      errorPayload = await res.json()
      message =
        errorPayload?.error ||
        errorPayload?.message ||
        (typeof errorPayload === 'string' ? errorPayload : '')
    } catch {
      message = await res.text()
    }
    const error = new Error(message || `Request failed (${res.status})`)
    error.status = res.status
    error.payload = errorPayload
    throw error
  }
  return res.json()
}
export {
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
  safeNumber,
  parsePriceValue,
  formatUsd,
  getPricing,
  getUsdPriceValue,
  getUsdPriceLabel,
  getListingId,
  extractListingIdFromUrl,
  extractAirbnbListingUrlsFromDragPayload,
  dragPayloadHasAirbnbSearchUrl,
  getListingUrl,
  getListingLocation,
  getListingRating,
  getListingCoordinates,
  getReviewCoverage,
  hasReviewCaptureGap,
  getComparisonCoverage,
  getCaptureStage,
  getCaptureStageLabel,
  getCaptureStageBadgeVariant,
  formatReviewCoverage,
  needsFullReviews,
  getCaptureStageGuidance,
  getValidation,
  getValidationLabel,
  getMetricValue,
  getAmenityLabel,
  getAmenitiesList,
  getPrimaryPhoto,
  getPhotoUrl,
  getPhotoAreaSummary,
  getRepresentativePhotos,
  getPreferenceAlignment,
  getPreferenceScore,
  hasPreferenceAlignment,
  getPreferenceAlignmentLabel,
  getDateContext,
  getDateMatchType,
  getListingDatePair,
  getRequestedDatePair,
  formatDateRange,
  getDateContextLabel,
  getDateContextVariant,
  formatJobLabel,
  requestJson,
}
