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
