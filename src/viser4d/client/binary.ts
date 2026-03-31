import * as msgpack from "@msgpack/msgpack";

export type RuntimeScalar = string | number | boolean | null;

export type RuntimeValue =
  | RuntimeScalar
  | ArrayBuffer
  | ArrayBufferView
  | RuntimeValue[]
  | { [key: string]: RuntimeValue | undefined };

export type RuntimeMessage = {
  type: string;
  [key: string]: RuntimeValue | undefined;
};

type HybridPayload = {
  binaryBufferLengths?: number[];
  [key: string]: unknown;
};

const DTYPE_CONSTRUCTORS: Record<
  string,
  {
    ctor: new (
      buffer: ArrayBufferLike,
      byteOffset: number,
      length: number,
    ) => ArrayBufferView;
    bytes: number;
  }
> = {
  "<f2": { ctor: Uint16Array, bytes: 2 },
  "<f4": { ctor: Float32Array, bytes: 4 },
  "<f8": { ctor: Float64Array, bytes: 8 },
  "|u1": { ctor: Uint8Array, bytes: 1 },
  "<u2": { ctor: Uint16Array, bytes: 2 },
  "<u4": { ctor: Uint32Array, bytes: 4 },
  "|i1": { ctor: Int8Array, bytes: 1 },
  "<i2": { ctor: Int16Array, bytes: 2 },
  "<i4": { ctor: Int32Array, bytes: 4 },
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

function asBinaryView(value: object): ArrayBufferView | Uint8Array | null {
  if (value instanceof ArrayBuffer) {
    return new Uint8Array(value);
  }
  if (!ArrayBuffer.isView(value)) {
    return null;
  }
  return value;
}

function computeBinaryOffsets(
  bufferLengths: number[],
  baseOffset: number,
): number[] {
  const offsets: number[] = [];
  let offset = baseOffset;
  for (const length of bufferLengths) {
    offset += (8 - (offset % 8)) % 8;
    offsets.push(offset);
    offset += length;
  }
  return offsets;
}

function replaceBinaryPlaceholders(
  obj: unknown,
  buffer: ArrayBuffer,
  binaryOffsets: number[],
  bufferLengths: number[],
): unknown {
  if (obj === null || obj === undefined) {
    return obj;
  }
  if (Array.isArray(obj)) {
    for (let i = 0; i < obj.length; i += 1) {
      obj[i] = replaceBinaryPlaceholders(
        obj[i],
        buffer,
        binaryOffsets,
        bufferLengths,
      );
    }
    return obj;
  }
  if (typeof obj !== "object" || ArrayBuffer.isView(obj)) {
    return obj;
  }

  const record = obj as Record<string, unknown>;
  const idx = record.__binary_index;
  const dtype = record.dtype;
  if (typeof idx === "number" && typeof dtype === "string") {
    const offset = binaryOffsets[idx];
    const byteLength = bufferLengths[idx];
    const dtypeInfo = DTYPE_CONSTRUCTORS[dtype];
    if (dtypeInfo) {
      return new dtypeInfo.ctor(buffer, offset, byteLength / dtypeInfo.bytes);
    }
    return new Uint8Array(buffer, offset, byteLength);
  }

  for (const [key, inner] of Object.entries(record)) {
    record[key] = replaceBinaryPlaceholders(
      inner,
      buffer,
      binaryOffsets,
      bufferLengths,
    );
  }
  return record;
}

export function decodeHybridPayloadBase64<T>(base64Text: string): T {
  const bytes = decodeBase64Bytes(base64Text);
  const buffer = bytes.buffer as ArrayBuffer;
  const baseOffset = bytes.byteOffset;
  const msgpackLength = Number(
    new DataView(buffer, baseOffset, 8).getBigUint64(0, true),
  );
  const msgpackData = new Uint8Array(buffer, baseOffset + 8, msgpackLength);
  const payload = msgpack.decode(msgpackData) as T & HybridPayload;
  const bufferLengths = payload.binaryBufferLengths;
  if (bufferLengths && bufferLengths.length > 0) {
    const binaryOffsets = computeBinaryOffsets(
      bufferLengths,
      baseOffset + 8 + msgpackLength,
    );
    replaceBinaryPlaceholders(payload, buffer, binaryOffsets, bufferLengths);
    delete payload.binaryBufferLengths;
  }
  return payload;
}

export function normalizeTransportValue(value: RuntimeValue): RuntimeValue {
  if (Array.isArray(value)) {
    return value.map((item) => normalizeTransportValue(item));
  }
  if (!value || typeof value !== "object") {
    return value;
  }
  const binaryView = asBinaryView(value);
  if (binaryView) {
    return binaryView;
  }
  const out: { [key: string]: RuntimeValue | undefined } = {};
  for (const [key, inner] of Object.entries(value as Record<string, unknown>)) {
    out[key] =
      inner === undefined
        ? undefined
        : normalizeTransportValue(inner as RuntimeValue);
  }
  return out;
}

export function normalizeTransportMessage(message: RuntimeMessage): RuntimeMessage {
  return normalizeTransportValue(message) as RuntimeMessage;
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
