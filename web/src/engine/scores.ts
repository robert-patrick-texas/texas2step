// Interpretation layer: bufferbloat grade and use-case readiness.
// Thresholds are documented in docs/methodology.md.
import type { Bufferbloat, CallRating, Level, Readiness, TestResult } from "../types";

const BLOAT: [string, number][] = [
  ["A+", 5],
  ["A", 30],
  ["B", 60],
  ["C", 200],
  ["D", 400],
];

export function bufferbloatGrade(addedMs: number | null): string | null {
  if (addedMs === null) return null;
  for (const [grade, limit] of BLOAT) if (addedMs < limit) return grade;
  return "F";
}

export function bufferbloat(r: TestResult): Bufferbloat {
  const idle = r.latency?.median ?? null;
  const added = (loaded: number | null | undefined) =>
    idle !== null && loaded !== null && loaded !== undefined ? loaded - idle : null;
  const down = added(r.download?.loaded_latency?.median);
  const up = added(r.upload?.loaded_latency?.median);
  const worst = [down, up].filter((v): v is number => v !== null);
  return {
    added_download_ms: down,
    added_upload_ms: up,
    grade: bufferbloatGrade(worst.length ? Math.max(...worst) : null),
  };
}

/** The grade withheld ("device busy") where the page's own lateness made most of a rise
 * that graded B or worse, and moved the grade: how much it added under load, beyond idle,
 * from `inflation`. An A or A+ stands: the lateness could only have made it look worse. */
export function withheld(b: Bufferbloat, inflation: { download: number; upload: number }): Bufferbloat {
  const sides = [
    [b.added_download_ms, inflation.download],
    [b.added_upload_ms, inflation.upload],
  ].filter((s): s is [number, number] => s[0] !== null);
  if (!sides.length || b.grade === null || b.grade.startsWith("A")) return b;
  const worst = sides.reduce((a, c) => (c[0] > a[0] ? c : a));
  const corrected = Math.max(...sides.map(([added, lag]) => Math.max(0, added - lag)));
  if (worst[1] * 2 >= worst[0] && bufferbloatGrade(corrected) !== b.grade) {
    return { ...b, grade: null, withheld: "device busy" };
  }
  return b;
}

interface Metrics {
  down?: number;
  up?: number;
  latency?: number;
  jitter?: number;
  loss?: number;
  bloat?: number;
  call?: number; // the call-quality rating, 0 bad to 4 excellent
}

type Need = Partial<Record<keyof Metrics, number>>;

// For each use case: the minimum bar for great / good / fair. Rates are floors;
// latency, jitter, loss and bloat are ceilings.
const CASES: { id: Readiness["id"]; label: string; tiers: [Need, Need, Need] }[] = [
  {
    id: "streaming",
    label: "4K video streaming",
    tiers: [
      { down: 25, loss: 1, latency: 150 },
      { down: 15, loss: 2 },
      { down: 5, loss: 5 },
    ],
  },
  {
    id: "calls",
    label: "Video calls",
    tiers: [
      { down: 10, up: 5, latency: 50, jitter: 10, loss: 0.5, bloat: 60, call: 4 },
      { down: 4, up: 2, latency: 100, jitter: 30, loss: 1.5, bloat: 200, call: 3 },
      { down: 1.5, up: 1, latency: 200, jitter: 50, loss: 3, call: 2 },
    ],
  },
  {
    id: "interactive",
    label: "Reaction time, VDI and SSH",
    tiers: [
      { down: 10, up: 3, latency: 20, jitter: 5, loss: 0.1, bloat: 30 },
      { down: 5, up: 1, latency: 50, jitter: 15, loss: 1, bloat: 100 },
      { latency: 100, jitter: 30, loss: 2 },
    ],
  },
  {
    id: "transfers",
    label: "Large file transfers",
    tiers: [
      { down: 500, up: 500 },
      { down: 100, up: 50 },
      { down: 25, up: 10 },
    ],
  },
];

const FLOORS = new Set<keyof Metrics>(["down", "up", "call"]);
const RANK: Record<CallRating, number> = { bad: 0, poor: 1, fair: 2, good: 3, excellent: 4 };
const LABEL: Record<keyof Metrics, string> = {
  down: "download",
  up: "upload",
  latency: "latency",
  jitter: "jitter",
  loss: "packet loss",
  bloat: "latency under load",
  call: "call quality",
};

function unmet(m: Metrics, need: Need): (keyof Metrics)[] {
  return (Object.keys(need) as (keyof Metrics)[]).filter((k) => {
    const have = m[k];
    if (have === undefined) return false;
    return FLOORS.has(k) ? have < need[k]! : have > need[k]!;
  });
}

export function readiness(r: TestResult): Readiness[] {
  const bloat = r.bufferbloat ?? bufferbloat(r);
  const added = [bloat.added_download_ms, bloat.added_upload_ms].filter((v): v is number => v !== null);
  const m: Metrics = {
    down: r.download?.mbps,
    up: r.upload?.mbps,
    latency: r.latency?.median ?? undefined,
    jitter: r.latency?.jitter ?? undefined,
    loss: r.loss && !r.loss.error ? r.loss.loss_pct : undefined,
    bloat: added.length && !bloat.withheld ? Math.max(...added) : undefined,
    call: r.call ? RANK[r.call.rating] : undefined,
  };
  const levels: Level[] = ["great", "good", "fair"];
  return CASES.map(({ id, label, tiers }) => {
    if (Object.values(m).every((v) => v === undefined)) return { id, label, level: "unknown", detail: "No data" };
    for (let i = 0; i < tiers.length; i++) {
      if (unmet(m, tiers[i]!).length === 0) {
        const detail = i === 0 ? "Comfortably within requirements" : `Limited by ${unmet(m, tiers[i - 1]!).map((k) => LABEL[k]).join(", ")}`;
        return { id, label, level: levels[i]!, detail };
      }
    }
    return { id, label, level: "poor", detail: `Limited by ${unmet(m, tiers[2]).map((k) => LABEL[k]).join(", ")}` };
  });
}
