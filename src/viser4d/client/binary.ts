// Plain-data helpers shared across the runtime. No viser coupling lives here.

/**
 * A viser scene message in dict form. `protocol.gen.ts` references this type by
 * `import("./binary").ScenePayload`. Numpy arrays inside a payload arrive as
 * raw bytes (msgpack bin); anything that needs interpretation carries its own
 * metadata (see {@link WaveformPayload}).
 */
export type ScenePayload = {
  type: string;
  name?: string;
  [key: string]: unknown;
};

/** Flat float32 samples, frame-major: `data[frame * numChannels + channel]`. */
export type WaveformPayload = {
  numChannels: number;
  numFrames: number;
  data: Uint8Array;
};

/**
 * Reinterpret waveform bytes as a `Float32Array`. msgpack `bin` buffers can land
 * at a byte offset that is not a multiple of 4, which `Float32Array`'s buffer
 * view refuses; copy to a fresh aligned buffer only in that case.
 */
export function waveformFloats(data: Uint8Array): Float32Array {
  if (data.byteOffset % 4 === 0) {
    return new Float32Array(data.buffer, data.byteOffset, data.byteLength >> 2);
  }
  const aligned = new Uint8Array(data.byteLength);
  aligned.set(data);
  return new Float32Array(aligned.buffer);
}
