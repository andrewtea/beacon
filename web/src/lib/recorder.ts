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
  // From here the microphone is held. It is let go of the moment the tap is
  // called off, whatever is being waited for then, and again when this function
  // ends, however it ends: everything that can fail is inside the block whose
  // `finally` does it, the making of the audio context included.
  const letGo = () => {
    for (const track of stream.getTracks()) track.stop()
  }
  let context: AudioContext | null = null
  let module: string | null = null
  let late: ReturnType<typeof setTimeout> | undefined
  let calledOff: (() => void) | undefined
  const aborted = new Promise<never>((_resolve, reject) => {
    calledOff = () => {
      letGo()
      reject(new DOMException('Aborted', 'AbortError'))
    }
  })
  // A tap that is called off while nothing is waiting on `aborted` is not an
  // error nobody handled.
  aborted.catch(() => undefined)
  try {
    // The tap can have been called off while the browser asked for permission.
    if (signal?.aborted) calledOff?.()
    else if (calledOff !== undefined)
      signal?.addEventListener('abort', calledOff, { once: true })
    // Each step that waits is raced against the tap being called off, so that
    // nothing more is set up once it is. `aborted` is named first: of two
    // promises that are both settled, the race goes to the first.
    const unlessCalledOff = <T>(step: Promise<T>): Promise<T> =>
      Promise.race([aborted, step])
    await unlessCalledOff(Promise.resolve())
    context = new AudioContext()
    module = URL.createObjectURL(
      new Blob([WORKLET], { type: 'text/javascript' }),
    )
    await unlessCalledOff(context.audioWorklet.addModule(module))
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
    const recorded = new Promise<void>((resolve, reject) => {
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
    })
    source.connect(node)
    // Starting the context is waited for with the samples, so that a context
    // that will not start ends the recording, and lets go of the microphone,
    // as a timeout or a tap that is called off does.
    await unlessCalledOff(Promise.all([context.resume(), recorded]))
    return encodeWav(samples, rate)
  } catch (error) {
    // A tap that was called off is its caller's own doing, and theirs to tell
    // apart. Anything else the browser raised is a clip that was not recorded.
    if (error instanceof ClipError) throw error
    if (error instanceof DOMException && error.name === 'AbortError')
      throw error
    throw new ClipError('Beacon could not record a clip.')
  } finally {
    letGo()
    clearTimeout(late)
    if (calledOff !== undefined) signal?.removeEventListener('abort', calledOff)
    if (module !== null) URL.revokeObjectURL(module)
    // Closing a context that is already closed is refused, and is no failure.
    if (context !== null) void context.close().catch(() => undefined)
  }
}
