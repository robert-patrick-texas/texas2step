// Number formatting: values and units are returned separately so the UI can style them.

export interface Formatted {
  value: string;
  unit: string;
}

const nf = (digits: number) =>
  new Intl.NumberFormat(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });

export function rate(mbps: number | null | undefined): Formatted {
  if (mbps === null || mbps === undefined || !Number.isFinite(mbps)) return { value: "—", unit: "Mbps" };
  if (mbps >= 1000) return { value: nf(mbps >= 10000 ? 1 : 2).format(mbps / 1000), unit: "Gbps" };
  if (mbps >= 100) return { value: nf(0).format(mbps), unit: "Mbps" };
  if (mbps >= 10) return { value: nf(1).format(mbps), unit: "Mbps" };
  return { value: nf(2).format(mbps), unit: "Mbps" };
}

export function ms(value: number | null | undefined): Formatted {
  if (value === null || value === undefined || !Number.isFinite(value)) return { value: "—", unit: "ms" };
  if (value >= 100) return { value: nf(0).format(value), unit: "ms" };
  if (value >= 10) return { value: nf(1).format(value), unit: "ms" };
  return { value: nf(2).format(value), unit: "ms" };
}

export function pct(value: number | null | undefined): Formatted {
  if (value === null || value === undefined || !Number.isFinite(value)) return { value: "—", unit: "%" };
  return { value: nf(value === 0 ? 0 : value < 1 ? 2 : 1).format(value), unit: "%" };
}

export const text = (f: Formatted) => (f.value === "—" ? "—" : `${f.value} ${f.unit}`);

export function bytes(n: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1000 && i < units.length - 1) {
    n /= 1000;
    i++;
  }
  return `${nf(i === 0 ? 0 : 1).format(n)} ${units[i]}`;
}

export function when(epochOrIso: number | string): string {
  const d = typeof epochOrIso === "number" ? new Date(epochOrIso * 1000) : new Date(epochOrIso);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** Clean axis ticks: 1, 2, 2.5 or 5 × 10^n steps. */
export function niceTicks(max: number, target = 5): number[] {
  if (!(max > 0)) return [0, 1];
  const raw = max / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag;
  const ticks: number[] = [];
  for (let v = 0; v <= max + step * 0.999; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
  return ticks;
}

/** Axis tick for a Mbps scale: 0, 500M, 2G … */
export function mbpsTick(v: number): string {
  if (v === 0) return "0";
  return v >= 1000 ? `${compact(v / 1000)}G` : `${compact(v)}M`;
}

export function compact(v: number): string {
  if (v >= 1000) return `${nf(v % 1000 === 0 ? 0 : 1).format(v / 1000)}K`;
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(v);
}
