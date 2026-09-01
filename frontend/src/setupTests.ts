import '@testing-library/jest-dom'

// Node 20+ defines a global `localStorage`/`sessionStorage` (inert without
// the --localstorage-file flag) that already exists on globalThis by the time
// vitest's jsdom environment runs. Vitest's window->global copy skips keys
// that already exist on the target global, so jsdom's real, spec-compliant
// Storage-backed implementation never gets installed and the inert Node
// stand-in shadows it instead -- `localStorage.setItem` throws in every test.
// Rebind both to the actual jsdom window's storage objects so tests exercise
// the same Web Storage API surface a real browser provides.
for (const key of ['localStorage', 'sessionStorage'] as const) {
  Object.defineProperty(globalThis, key, {
    get: () => (globalThis as unknown as { jsdom: { window: Window } }).jsdom.window[key],
    configurable: true,
  })
}
