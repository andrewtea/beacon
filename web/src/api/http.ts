/** A request the server answered with an error, or did not answer. */
export class ApiError extends Error {
  // The HTTP status, or null when the server could not be reached.
  readonly status: number | null

  constructor(message: string, status: number | null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/** The header in which a tap, or a request about saved places, names the mode
 * its sender was shown: what the app held to be the server's mode when the
 * person pressed. */
export const MODE_HEADER = 'Beacon-Mode'
/** The status a server in the other mode refuses such a request with, unread. */
export const OTHER_MODE = 409

// Named by a tap that holds a clip or a photo, when its sender was shown the
// server's sentence about where those go (`media_text` of the health): `meta`.
// A server that sends either to Meta refuses, with `OTHER_MODE`'s status, a tap
// that holds one and does not say so: what the page was showing is not what the
// server does now, and the page asks again.
export const MEDIA_HEADER = 'Beacon-Media'
export const MEDIA_TOLD = 'meta'

/** The JSON a route answers with. A reply that is not 2xx is an ApiError. */
export async function fetchJson<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, init)
  } catch (error) {
    // An aborted request is the caller's own doing, and theirs to tell apart.
    if (error instanceof DOMException && error.name === 'AbortError')
      throw error
    throw new ApiError('The server could not be reached.', null)
  }
  if (!response.ok) {
    // The server's routes say what went wrong in `detail`, in a fixed sentence.
    const body: unknown = await response.json().catch(() => null)
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body
        ? body.detail
        : null
    throw new ApiError(
      typeof detail === 'string'
        ? detail
        : `The server answered with HTTP ${response.status}.`,
      response.status,
    )
  }
  return (await response.json()) as T
}
