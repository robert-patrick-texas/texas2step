// Shapes shared with the Python server (snake_case on purpose: results round-trip
// through /api/v1/results and the CLI emits the same structure).

export interface LatencyStats {
  count: number;
  lost: number;
  min: number | null;
  max: number | null;
  mean: number | null;
  median: number | null;
  p25: number | null;
  p75: number | null;
  p90: number | null;
  stdev: number | null;
  jitter: number | null;
  samples?: number[];
}

export type Direction = "download" | "upload";

export interface ThroughputResult {
  direction: Direction;
  mbps: number;
  peak_mbps: number;
  bytes: number;
  duration_s: number;
  warmup_s: number;
  bin_ms: number;
  timeline: number[];
  streams: number;
  per_stream_mbps: number[];
  loaded_latency: LatencyStats | null;
}

export interface LossResult {
  transport: "webrtc" | "websocket";
  sent: number;
  received: number;
  lost: number;
  loss_pct: number;
  up_loss_pct: number | null;
  down_loss_pct: number | null;
  out_of_order: number;
  duplicates: number;
  interval_ms: number;
  size: number;
  rtt: LatencyStats;
  packets: (number | null)[];
  note?: string;
  error?: string;
}

export type Level = "great" | "good" | "fair" | "poor" | "unknown";

export interface Readiness {
  id: "streaming" | "calls" | "gaming" | "transfers";
  label: string;
  level: Level;
  detail: string;
}

export interface Bufferbloat {
  added_download_ms: number | null;
  added_upload_ms: number | null;
  grade: string | null;
}

export interface TestResult {
  version: 1;
  id?: string;
  saved_at?: number;
  tool: string;
  started_at: string;
  duration_s?: number;
  server: { id?: string; name?: string; location?: string; url: string; version?: string };
  client: { ip?: string; family?: string | null; protocol?: string; user_agent?: string };
  config: { streams: number; seconds: number; packets: number; interval_ms: number };
  latency?: LatencyStats;
  download?: ThroughputResult;
  upload?: ThroughputResult;
  loss?: LossResult;
  bufferbloat?: Bufferbloat;
  readiness?: Readiness[];
}

export interface ServerInfo {
  name: string;
  version: string;
  server: { id: string; name: string; location: string };
  capabilities: { websocket: boolean; webrtc: boolean; results: boolean; measure: boolean };
  limits: { max_download_bytes: number; max_upload_bytes: number; max_stream_seconds: number };
  ice_servers: RTCIceServer[];
  defaults: { streams: number; seconds: number; packets: number };
}

export interface WhoAmI {
  ip: string;
  port: number | null;
  family: string | null;
  scheme: string;
  http_version: string;
  user_agent: string | null;
  via_proxy: boolean;
}

export interface Location {
  id: string;
  name: string;
  url: string;
  location: string;
  url_v4: string;
  url_v6: string;
}

export interface ResultSummary {
  id: string;
  created_at: number;
  client_ip: string | null;
  server_id: string | null;
  source: string | null;
  download_mbps: number | null;
  upload_mbps: number | null;
  latency_ms: number | null;
  jitter_ms: number | null;
  loss_pct: number | null;
}
