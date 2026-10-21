// This computer's side of a path: whether the browser revealed a real address, and the MAC
// address an EUI-64 IPv6 address carries.

/**
 * An IPv4 or IPv6 address as WebRTC reported it, or null where the browser hid it. By
 * default browsers report an empty address, or a random name ending in ".local", instead.
 */
export function revealed(address: string | null | undefined): string | null {
  if (!address || address === "0.0.0.0" || address === "::") return null;
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(address)) return address;
  return bytes6(address) ? address : null;
}

/**
 * The MAC address in an IPv6 address formed by EUI-64 (RFC 4291): its interface ID is the
 * MAC with ff:fe in the middle and the universal/local bit inverted. The privacy and
 * stable random addresses that current desktop and phone systems use carry none.
 */
export function eui64Mac(ip: string | null | undefined): string | null {
  const b = ip ? bytes6(ip) : null;
  if (!b || b[11] !== 0xff || b[12] !== 0xfe) return null;
  return [b[8]! ^ 0x02, b[9]!, b[10]!, b[13]!, b[14]!, b[15]!].map((x) => x.toString(16).padStart(2, "0")).join(":");
}

/** The 16 bytes of an IPv6 address (any zone dropped), or null if it isn't one. */
function bytes6(ip: string): number[] | null {
  const halves = ip.replace(/%.*$/, "").toLowerCase().split("::");
  if (halves.length > 2) return null;
  const head = halves[0] ? halves[0].split(":") : [];
  const tail = halves[1] ? halves[1].split(":") : [];
  const gap = 8 - head.length - tail.length;
  if (halves.length === 1 ? gap !== 0 : gap < 1) return null;
  const words = [...head, ...Array<string>(halves.length === 2 ? gap : 0).fill("0"), ...tail];
  if (!words.every((w) => /^[0-9a-f]{1,4}$/.test(w))) return null;
  return words.flatMap((w) => [parseInt(w, 16) >> 8, parseInt(w, 16) & 0xff]);
}
