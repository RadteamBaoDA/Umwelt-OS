/** Millisecond timestamp of the last issued id; a backwards clock is clamped to it. */
let lastMs = 0;
/** 12-bit counter stored in `rand_a`, strictly increasing within one millisecond. */
let counter = 0;

/**
 * Generates a time-ordered UUID version 7 (RFC 9562), mirroring the backend `core.ids.uuid7`.
 *
 * Layout: 48-bit unix milliseconds, version 7, a 12-bit counter that is strictly monotonic within a
 * millisecond (on overflow the millisecond advances by one), RFC variant bits and a 62-bit random tail
 * from `crypto.getRandomValues`. The timestamp is visible in the id, which is acceptable for this
 * single-user app. Ordering is only guaranteed within this browsing context.
 *
 * @returns A lowercase 36-character UUID string.
 */
export function uuid7(): string {
  const random = new Uint8Array(10);
  crypto.getRandomValues(random);
  const now = Date.now();
  if (now > lastMs) {
    lastMs = now;
    // Start below the maximum so a burst in one millisecond has headroom before overflow.
    counter = ((random[8] << 8) | random[9]) & 0x7ff;
  } else {
    counter += 1;
    if (counter > 0xfff) {
      lastMs += 1;
      counter = 0;
    }
  }
  const bytes = new Uint8Array(16);
  const high = Math.floor(lastMs / 0x100000000);
  const low = lastMs % 0x100000000;
  bytes[0] = (high >>> 8) & 0xff;
  bytes[1] = high & 0xff;
  bytes[2] = (low >>> 24) & 0xff;
  bytes[3] = (low >>> 16) & 0xff;
  bytes[4] = (low >>> 8) & 0xff;
  bytes[5] = low & 0xff;
  bytes[6] = 0x70 | (counter >>> 8);
  bytes[7] = counter & 0xff;
  bytes[8] = 0x80 | (random[0] & 0x3f);
  bytes.set(random.subarray(1, 8), 9);
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
