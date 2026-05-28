import { useEffect, useState } from 'react'

const resolveDefault = (defaultValue) =>
  typeof defaultValue === 'function' ? defaultValue() : defaultValue

export function useLocalStorageState(key, defaultValue, options = {}) {
  const { deserialize = JSON.parse, serialize = JSON.stringify } = options
  const [value, setValue] = useState(() => {
    if (typeof window === 'undefined') return resolveDefault(defaultValue)
    try {
      const stored = window.localStorage.getItem(key)
      if (stored === null) return resolveDefault(defaultValue)
      return deserialize(stored)
    } catch {
      return resolveDefault(defaultValue)
    }
  })

  useEffect(() => {
    if (typeof window === 'undefined') return
    try {
      window.localStorage.setItem(key, serialize(value))
    } catch {
      // Ignore persistence failures so UI state still works.
    }
  }, [key, serialize, value])

  return [value, setValue]
}
