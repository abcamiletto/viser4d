import type { TimelineBlockMessage } from "./protocol.gen";
import type { SceneEntry } from "./state";

export type ScenePayload = {
  type: string;
  name?: string;
  [key: string]: unknown;
};

export type Recording = {
  numSteps: number;
  fps: number;
  block: TimelineBlockMessage;
  overrides: SceneEntry[];
};

// The exporter writes explicit numpy dtypes. Float audio decoding is owned by
// viser-audio; this decoder also handles integer and float geometry attributes.
const ARRAY_TYPES = {
  "|b1": Uint8Array,
  "|u1": Uint8Array,
  "|i1": Int8Array,
  "<u2": Uint16Array,
  "<i2": Int16Array,
  "<u4": Uint32Array,
  "<i4": Int32Array,
  "<u8": BigUint64Array,
  "<i8": BigInt64Array,
  "<f2": Uint16Array,
  "<f4": Float32Array,
  "<f8": Float64Array,
};

export function decodeRecording(payload: string): Recording {
  return JSON.parse(payload, (_key, value) => {
    if (!value || typeof value !== "object" || !("__typed_array" in value)) return value;
    const Constructor = ARRAY_TYPES[value.__typed_array as keyof typeof ARRAY_TYPES];
    if (!Constructor) throw new Error(`Unsupported recording array dtype: ${value.__typed_array}`);
    const raw = atob(value.base64);
    const bytes = Uint8Array.from(raw, (char) => char.charCodeAt(0));
    return new Constructor(bytes.buffer);
  });
}
