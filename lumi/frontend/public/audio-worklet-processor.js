/**
 * AudioWorklet processor for capturing microphone input at 16kHz PCM.
 *
 * Runs in a dedicated audio thread. Receives samples from getUserMedia
 * (typically 44.1kHz or 48kHz), downsamples to 16kHz using linear
 * interpolation, converts to 16-bit signed integer PCM, and posts
 * 20ms frames (320 samples) to the main thread via port.postMessage.
 *
 * The main thread sends the source sample rate via port.postMessage
 * after getUserMedia resolves, which triggers the processor to
 * calculate the downsample ratio and begin accumulating frames.
 *
 * Validates: Requirements 7.4, 1.3
 */

// Target sample rate for Nova Sonic input (16kHz/16-bit/mono PCM)
const TARGET_SAMPLE_RATE = 16000;

// 20ms frame at 16kHz = 320 samples
const FRAME_SIZE = 320;

class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();

    // Source sample rate from the main thread (set via port.onmessage)
    this._sourceSampleRate = 0;

    // Ratio of source rate to target rate (e.g., 48000/16000 = 3.0)
    this._downsampleRatio = 0;

    // Number of source samples needed to produce one 320-sample frame
    this._sourceSamplesPerFrame = 0;

    // Internal buffer accumulating incoming source-rate samples
    this._buffer = [];

    // Whether the processor is initialized and ready to emit frames
    this._initialized = false;

    // Listen for the source sample rate from the main thread
    this.port.onmessage = (event) => {
      if (event.data && event.data.type === 'init') {
        this._sourceSampleRate = event.data.sampleRate;
        this._downsampleRatio = this._sourceSampleRate / TARGET_SAMPLE_RATE;
        this._sourceSamplesPerFrame = Math.ceil(FRAME_SIZE * this._downsampleRatio);
        this._initialized = true;
      }
    };
  }

  /**
   * Called by the audio thread ~128 samples at a time at the source sample rate.
   * Accumulates samples, downsamples to 16kHz, and emits 320-sample PCM frames.
   *
   * @param {Float32Array[][]} inputs - Input audio buffers (channel data)
   * @param {Float32Array[][]} outputs - Output audio buffers (unused)
   * @param {Record<string, Float32Array>} parameters - AudioParam values (unused)
   * @returns {boolean} true to keep the processor alive
   */
  process(inputs, outputs, parameters) {
    // Wait until the main thread has sent the source sample rate
    if (!this._initialized) {
      return true;
    }

    // Get the first channel of the first input
    const input = inputs[0];
    if (!input || input.length === 0) {
      return true;
    }

    const channelData = input[0];
    if (!channelData || channelData.length === 0) {
      return true;
    }

    // Accumulate incoming source-rate samples
    for (let i = 0; i < channelData.length; i++) {
      this._buffer.push(channelData[i]);
    }

    // Emit frames whenever we have enough source samples for a 20ms frame
    while (this._buffer.length >= this._sourceSamplesPerFrame) {
      // Extract enough source samples for one frame
      const sourceChunk = this._buffer.splice(0, this._sourceSamplesPerFrame);

      // Downsample using linear interpolation and convert to Int16 PCM
      const pcmFrame = this._downsampleAndConvert(sourceChunk);

      // Post the Int16 PCM buffer to the main thread
      this.port.postMessage(pcmFrame.buffer, [pcmFrame.buffer]);
    }

    return true;
  }

  /**
   * Downsamples source audio to 16kHz using linear interpolation,
   * then converts float32 samples to 16-bit signed integer PCM.
   *
   * For each target sample position, finds the corresponding position
   * in the source buffer and interpolates between the two nearest
   * neighbor samples for smooth output.
   *
   * @param {number[]} sourceChunk - Source-rate float32 samples (-1.0 to 1.0)
   * @returns {Int16Array} 320-sample Int16 PCM frame
   */
  _downsampleAndConvert(sourceChunk) {
    const output = new Int16Array(FRAME_SIZE);

    for (let i = 0; i < FRAME_SIZE; i++) {
      // Find the fractional position in the source buffer for this target sample
      const srcPosition = i * this._downsampleRatio;

      // Get the two nearest source sample indices
      const srcIndexLow = Math.floor(srcPosition);
      const srcIndexHigh = Math.min(srcIndexLow + 1, sourceChunk.length - 1);

      // Fractional distance between the two neighbors
      const fraction = srcPosition - srcIndexLow;

      // Linear interpolation between the two nearest samples
      const interpolated =
        sourceChunk[srcIndexLow] * (1 - fraction) +
        sourceChunk[srcIndexHigh] * fraction;

      // Clamp to valid float range before conversion
      const clamped = Math.max(-1.0, Math.min(1.0, interpolated));

      // Convert float32 (-1.0 to 1.0) to Int16 (-32768 to 32767)
      output[i] = clamped < 0
        ? Math.round(clamped * 32768)
        : Math.round(clamped * 32767);
    }

    return output;
  }
}

// Register the processor so it can be loaded via:
// audioContext.audioWorklet.addModule('/audio-worklet-processor.js')
registerProcessor('pcm-capture-processor', PcmCaptureProcessor);
