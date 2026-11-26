// The server takes a clip only as a WAV file of plain samples (8 or 16 bits, at
// 8 to 48 kHz), and a browser's own recorder writes other formats. So the app
// records samples and writes the WAV itself.

export const MAX_RATE = 48_000

/** Every `step`th sample, for a microphone that runs faster than a clip may. */
export function decimate(samples: Float32Array, step: number): Float32Array {
  if (step <= 1) return samples
  const kept = new Float32Array(Math.floor(samples.length / step))
  for (let index = 0; index < kept.length; index++)
    kept[index] = samples[index * step]
  return kept
}

/** A mono, 16-bit WAV file of `samples`, which are between -1 and 1. */
export function encodeWav(samples: Float32Array, rate: number): Uint8Array {
  const step = Math.ceil(rate / MAX_RATE)
  const kept = decimate(samples, step)
  const sampleRate = Math.round(rate / step)
  const dataBytes = kept.length * 2
  const file = new DataView(new ArrayBuffer(44 + dataBytes))
  const ascii = (offset: number, text: string) => {
    for (let index = 0; index < text.length; index++)
      file.setUint8(offset + index, text.charCodeAt(index))
  }
  ascii(0, 'RIFF')
  file.setUint32(4, 36 + dataBytes, true)
  ascii(8, 'WAVE')
  ascii(12, 'fmt ')
  file.setUint32(16, 16, true) // the size of the format chunk
  file.setUint16(20, 1, true) // plain samples (PCM)
  file.setUint16(22, 1, true) // one channel
  file.setUint32(24, sampleRate, true)
  file.setUint32(28, sampleRate * 2, true) // bytes a second
  file.setUint16(32, 2, true) // bytes a sample
  file.setUint16(34, 16, true) // bits a sample
  ascii(36, 'data')
  file.setUint32(40, dataBytes, true)
  for (let index = 0; index < kept.length; index++) {
    const sample = Math.max(-1, Math.min(1, kept[index]))
    file.setInt16(44 + index * 2, Math.round(sample * 32767), true)
  }
  return new Uint8Array(file.buffer)
}

/** Bytes in base64, which is how the tap route takes a photo and a clip. */
export function toBase64(bytes: Uint8Array): string {
  let binary = ''
  const chunk = 0x8000
  for (let offset = 0; offset < bytes.length; offset += chunk)
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunk))
  return btoa(binary)
}
