// The server keeps a photo only as a new JPEG it draws from the one it is sent,
// and in doing so loses which way up the picture is and its colour profile. So
// the app draws the picture to a canvas first, which makes it upright and sRGB,
// and sends that: nothing of the camera's own file (its Exif, its GPS position)
// leaves the phone.

export const MAX_SIDE = 2048
// The server takes a JPEG of at most 4 MB.
export const MAX_BYTES = 4_000_000

/** Why a photo could not be attached, in a sentence a person can act on. */
export class PhotoError extends Error {}

/** The size a picture is drawn at: no side longer than `MAX_SIDE`. */
export function fitted(
  width: number,
  height: number,
): { width: number; height: number } {
  const scale = Math.min(1, MAX_SIDE / Math.max(width, height))
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  }
}

/** A new JPEG drawn from a picture the person chose, upright and no larger
 * than a tap may carry. */
export async function drawPhoto(file: Blob): Promise<Blob> {
  let picture: ImageBitmap
  try {
    picture = await createImageBitmap(file, { imageOrientation: 'from-image' })
  } catch {
    throw new PhotoError('Beacon could not read that picture.')
  }
  try {
    const { width, height } = fitted(picture.width, picture.height)
    const canvas = document.createElement('canvas')
    canvas.width = width
    canvas.height = height
    const context = canvas.getContext('2d')
    if (context === null)
      throw new PhotoError('This browser cannot draw a photo.')
    context.drawImage(picture, 0, 0, width, height)
    for (const quality of [0.85, 0.7, 0.5]) {
      const drawn = await new Promise<Blob | null>((resolve) =>
        canvas.toBlob(resolve, 'image/jpeg', quality),
      )
      if (drawn !== null && drawn.size <= MAX_BYTES) return drawn
    }
    throw new PhotoError('That picture is too large to send with a tap.')
  } finally {
    picture.close()
  }
}
