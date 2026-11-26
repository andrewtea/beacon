// A tap's id is what its sender reads the answer with, and it is in the path of
// the tap's own routes (`/api/taps/<id>`). The app names a tap only in the URL's
// fragment, which no server is sent, but the request for the answer has to name
// it, and anything that stands between the app and the server can write that
// request's path down. This takes the id out of a line before it is written.

const TAP_PATH = /(\/api\/taps)(?:\/|%2f)[^\s"'`<>]*/gi

/** `text` with whatever follows `/api/taps/` in a path left out. */
export function withoutTapIds(text: string): string {
  return text.replace(TAP_PATH, '$1/[left out]')
}
