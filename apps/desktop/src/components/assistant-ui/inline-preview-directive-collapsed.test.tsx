// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'

// The directive's frame resolves the file against the session cwd via
// session-view and local-preview; the collapsed card needs none of that until
// opened. Mock both so the test exercises the collapse gate itself.
const cwdStore = {
  get: () => '/w',
  listen: () => () => {},
  subscribe: () => () => {}
}

vi.mock('@/app/chat/session-view', () => ({
  useSessionView: () => ({ $cwd: cwdStore })
}))
vi.mock('@/lib/local-preview', () => ({
  localPreviewTarget: (file: string) => ({ path: `/w/${file}` })
}))
vi.mock('@/lib/media', () => ({
  isRemoteGateway: () => false
}))

import { InlinePreviewDirective } from './inline-preview-directive'

function renderDirective(node: ReactNode) {
  return render(
    <I18nProvider configClient={null} initialLocale="en">
      {node}
    </I18nProvider>
  )
}

describe('InlinePreviewDirective collapsed by default', () => {
  let readFileText: ReturnType<typeof vi.fn>

  beforeEach(() => {
    readFileText = vi.fn(async () => ({ text: '<html><body><h1>live</h1></body></html>', binary: false }))
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { readFileText }
    })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('renders the file card collapsed — no iframe, no file read, until "Ver"', () => {
    renderDirective(<InlinePreviewDirective attrs={{ file: 'demo.html' }} streaming={false} />)

    // Card shows the file name and the open button; the frame is NOT mounted.
    expect(screen.getByTitle('demo.html').textContent).toBe('demo.html')
    expect(screen.getByRole('button', { name: 'Open preview' })).toBeTruthy()
    expect(document.querySelector('iframe')).toBeNull()
    expect(readFileText).not.toHaveBeenCalled()
  })

  it('mounts the live frame only after the user clicks open', async () => {
    renderDirective(<InlinePreviewDirective attrs={{ file: 'demo.html' }} streaming={false} />)

    fireEvent.click(screen.getByRole('button', { name: 'Open preview' }))

    // The read happens on open (lazy gate) and the frame mounts with srcdoc.
    await waitFor(() => {
      expect(readFileText).toHaveBeenCalledWith('/w/demo.html')
    })

    const frame = await waitFor(() => {
      const el = document.querySelector('iframe')
      expect(el).not.toBeNull()

      return el as HTMLIFrameElement
    })

    expect(frame.getAttribute('sandbox')).toBe('allow-scripts')
    expect(frame.getAttribute('srcdoc')).toContain('<h1>live</h1>')
    // Toggle text flips to hide while open.
    expect(screen.getByRole('button', { name: 'Hide' })).toBeTruthy()
  })

  it('collapses the frame again on a second click', async () => {
    renderDirective(<InlinePreviewDirective attrs={{ file: 'demo.html' }} streaming={false} />)

    fireEvent.click(screen.getByRole('button', { name: 'Open preview' }))
    await waitFor(() => {
      expect(document.querySelector('iframe')).not.toBeNull()
    })

    fireEvent.click(screen.getByRole('button', { name: 'Hide' }))
    expect(document.querySelector('iframe')).toBeNull()
    expect(screen.getByRole('button', { name: 'Open preview' })).toBeTruthy()
  })
})
