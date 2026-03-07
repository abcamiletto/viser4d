export type RuntimeScalar = string | number | boolean | null;

export type RuntimeValue =
  | RuntimeScalar
  | Uint8Array
  | RuntimeValue[]
  | { [key: string]: RuntimeValue | undefined };

export type RuntimeMessage = {
  type: string;
  [key: string]: RuntimeValue | undefined;
};

type BinaryPayload = {
  __viser4d_binary__: string;
};

export type AudioArrayPayload = {
  data: string;
  dtype: "int16" | "int32" | "float64" | "float32" | "uint8";
  numChannels: number;
  numFrames: number;
};

export function decodeBase64Bytes(base64Text: string): Uint8Array {
  const binary = atob(base64Text);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

function isBinaryPayload(value: Record<string, unknown>): value is BinaryPayload {
  return typeof value.__viser4d_binary__ === "string";
}

export function revive(value: RuntimeValue): RuntimeValue {
  if (Array.isArray(value)) {
    return value.map((item) => revive(item));
  }
  if (!value || typeof value !== "object") {
    return value;
  }
  const record = value as Record<string, unknown>;
  if (isBinaryPayload(record)) {
    return decodeBase64Bytes(record.__viser4d_binary__);
  }
  const out: { [key: string]: RuntimeValue | undefined } = {};
  for (const [key, inner] of Object.entries(record)) {
    out[key] = inner === undefined ? undefined : revive(inner as RuntimeValue);
  }
  return out;
}

export function reviveMessage(message: RuntimeMessage): RuntimeMessage {
  return revive(message) as RuntimeMessage;
}

export function decodeAudioArray(payload: AudioArrayPayload): ArrayLike<number> {
  const buffer = decodeBase64Bytes(payload.data).buffer;
  switch (payload.dtype) {
    case "int16":
      return new Int16Array(buffer);
    case "int32":
      return new Int32Array(buffer);
    case "float64":
      return new Float64Array(buffer);
    case "float32":
      return new Float32Array(buffer);
    case "uint8":
      return new Uint8Array(buffer);
    default:
      return new Int16Array(buffer);
  }
}

export function samplesToFloat32(samples: ArrayLike<number>): Float32Array {
  if (samples instanceof Float32Array) {
    return samples;
  }
  if (samples instanceof Float64Array) {
    return Float32Array.from(samples);
  }
  if (samples instanceof Int16Array) {
    const out = new Float32Array(samples.length);
    for (let i = 0; i < samples.length; i += 1) {
      out[i] = (samples[i] ?? 0) / 32768;
    }
    return out;
  }
  if (samples instanceof Int32Array) {
    const out = new Float32Array(samples.length);
    for (let i = 0; i < samples.length; i += 1) {
      out[i] = (samples[i] ?? 0) / 2147483648;
    }
    return out;
  }
  if (samples instanceof Uint8Array) {
    const out = new Float32Array(samples.length);
    for (let i = 0; i < samples.length; i += 1) {
      out[i] = ((samples[i] ?? 128) - 128) / 128;
    }
    return out;
  }
  return Float32Array.from(samples);
}

export function decodeAudioWaveform(payload: AudioArrayPayload): Float32Array[] {
  const flat = samplesToFloat32(decodeAudioArray(payload));
  const channels: Float32Array[] = [];
  for (let channel = 0; channel < payload.numChannels; channel += 1) {
    const out = new Float32Array(payload.numFrames);
    for (let frame = 0; frame < payload.numFrames; frame += 1) {
      out[frame] = flat[frame * payload.numChannels + channel] ?? 0;
    }
    channels.push(out);
  }
  return channels;
}
