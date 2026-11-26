import { encodeWav } from './wav.ts'

export const CLIP_SECONDS = 5

/** Why a clip could not be recorded, in a sentence a person can act on. */
export class ClipError extends Error {}

// Runs on the audio thread and hands each block of samples to the page.
const WORKLET = `
registerProcessor('beacon-tap', class extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0]
    if (channel) this.port.postMessage(channel.slice(0))
    return true
  }
})`

/**
 * Records `seconds` of the microphone and answers with a WAV file. The samples
 * are held in memory and nowhere else, and the microphone is let go of when the
 * recording ends, is called off, or fails.
 */
export async function recordClip(
  seconds: number = CLIP_SECONDS,
  signal?: AbortSignal,
): Promise<Uint8Array> {
  if (
    navigator.mediaDevices?.getUserMedia === undefined ||
    typeof AudioContext === 'undefined' ||
    typeof AudioWorkletNode === 'undefined'
  )
    throw new ClipError('This browser cannot record a clip.')
  let stream: MediaStream
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true })
  } catch {
    throw new ClipError(
      'Beacon could not use the microphone. Allow it for this site to send a clip.',
    )
  }
  // From here the microphone is held, so everything that can fail is inside the
  // block that lets go of it, the making of the audio context included. Only
  // what was made is undone.
  let context: AudioContext | null = null
  let module: string | null = null
  let late: ReturnType<typeof setTimeout> | undefined
  let abort: (() => void) | undefined
  try {
    // The tap can have been called off while the browser asked for permission.
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    context = new AudioContext()
    module = URL.createObjectURL(
      new Blob([WORKLET], { type: 'text/javascript' }),
    )
    await context.audioWorklet.addModule(module)
    const node = new AudioWorkletNode(context, 'beacon-tap', {
      numberOfInputs: 1,
      numberOfOutputs: 0,
      channelCount: 1,
    })
    const rate = context.sampleRate
    const wanted = Math.round(seconds * rate)
    const samples = new Float32Array(wanted)
    let have = 0
    const source = context.createMediaStreamSource(stream)
    await new Promise<void>((resolve, reject) => {
      abort = () => reject(new DOMException('Aborted', 'AbortError'))
      if (signal?.aborted) return abort()
      signal?.addEventListener('abort', abort, { once: true })
      // A microphone that sends nothing does not hold the tap up for good.
      late = setTimeout(
        () => reject(new ClipError('The microphone sent no sound.')),
        (seconds + 5) * 1000,
      )
      node.port.onmessage = (event: MessageEvent<Float32Array>) => {
        const block = event.data.subarray(0, wanted - have)
        samples.set(block, have)
        have += block.length
        if (have >= wanted) resolve()
      }
      source.connect(node)
      void context?.resume()
    })
    return encodeWav(samples, rate)
  } catch (error) {
    if (error instanceof ClipError || error instanceof DOMException) throw error
    throw new ClipError('Beacon could not record a clip.')
  } finally {
    for (const track of stream.getTracks()) track.stop()
    clearTimeout(late)
    if (abort !== undefined) signal?.removeEventListener('abort', abort)
    if (module !== null) URL.revokeObjectURL(module)
    // Closing a context that is already closed is refused, and is no failure.
    if (context !== null) void context.close().catch(() => undefined)
  }
}
