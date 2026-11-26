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
  const context = new AudioContext()
  const module = URL.createObjectURL(
    new Blob([WORKLET], { type: 'text/javascript' }),
  )
  try {
    await context.audioWorklet.addModule(module)
    const node = new AudioWorkletNode(context, 'beacon-tap', {
      numberOfInputs: 1,
      numberOfOutputs: 0,
      channelCount: 1,
    })
    const wanted = Math.round(seconds * context.sampleRate)
    const samples = new Float32Array(wanted)
    let have = 0
    await new Promise<void>((resolve, reject) => {
      const abort = () => reject(new DOMException('Aborted', 'AbortError'))
      if (signal?.aborted) return abort()
      signal?.addEventListener('abort', abort, { once: true })
      // A microphone that sends nothing does not hold the tap up for good.
      const late = setTimeout(
        () => reject(new ClipError('The microphone sent no sound.')),
        (seconds + 5) * 1000,
      )
      node.port.onmessage = (event: MessageEvent<Float32Array>) => {
        const block = event.data.subarray(0, wanted - have)
        samples.set(block, have)
        have += block.length
        if (have >= wanted) {
          clearTimeout(late)
          resolve()
        }
      }
      context.createMediaStreamSource(stream).connect(node)
      void context.resume()
    })
    return encodeWav(samples, context.sampleRate)
  } catch (error) {
    if (error instanceof ClipError || error instanceof DOMException) throw error
    throw new ClipError('Beacon could not record a clip.')
  } finally {
    URL.revokeObjectURL(module)
    for (const track of stream.getTracks()) track.stop()
    void context.close()
  }
}
