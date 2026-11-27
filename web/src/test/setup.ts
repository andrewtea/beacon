import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach } from 'vitest'
import { fakeLocks } from './locks.ts'

// Every test has the browser's lock, as every browser the app runs in has. It
// is the navigator's own property, so a test that puts another navigator in its
// place by spreading this one carries the lock over.
beforeEach(() => {
  Object.defineProperty(window.navigator, 'locks', {
    value: fakeLocks(),
    configurable: true,
    enumerable: true,
    writable: true,
  })
})
afterEach(cleanup)
