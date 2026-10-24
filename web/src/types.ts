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

export type CallRating = "excellent" | "good" | "fair" | "poor" | "bad";

/** A call's quality over the path the packet-loss test measured (src/texas2step/quality.py). */
export interface CallQuality {
  basis: "round trip";
  packets: number; // in the call's view: the probes a call's 20 ms packets would be
  interval_ms: number; // their spacing
  late: number; // probes more than the jitter buffer behind the fastest
  late_pct: number;
  impaired_pct: number; // lost or late, of every probe
  burst_ratio: number | null; // 1 for random loss, more for bursts; null without any
  longest_gap_ms: number; // the longest run of lost or late packets in the call's view
  delay_ms: number; // mouth to ear: one way, plus what the call adds
  r: number; // the E-model's rating
  mos: number;
  mos_rating: CallRating; // the E-model's, from the score alone
  rating: CallRating; // as a caller would hear it: mos_rating, at most what the longest gap allows
}

/** Round trips per minute under load: each direction's, and the lower of the two. */
export interface Responsiveness {
  rpm: number;
  download_rpm: number | null;
  upload_rpm: number | null;
  basis: "latency under load";
}

export type Level = "great" | "good" | "fair" | "poor" | "unknown";

export interface Readiness {
  id: "streaming" | "calls" | "interactive" | "transfers";
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
  /** `local_ip`: this computer's own address: the browser's on the UDP path, where it
   *  reveals it, or the CLI's on its connection. `mac`: of the NIC the CLI's connection
   *  left by (`mac_source` "nic"), or read from an EUI-64 IPv6 address ("eui64"). */
  client: { ip?: string; family?: string | null; protocol?: string; user_agent?: string; local_ip?: string; mac?: string; mac_source?: "nic" | "eui64" };
  config: { streams: number; seconds: number; packets: number; interval_ms: number };
  latency?: LatencyStats;
  download?: ThroughputResult;
  upload?: ThroughputResult;
  loss?: LossResult;
  bufferbloat?: Bufferbloat;
  call?: CallQuality;
  responsiveness?: Responsiveness;
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
  /** With a traffic limit: whether this visitor may start a test; if not, the site's
   * message, and a link to continue elsewhere if it gave one. */
  traffic?: { open: boolean; message: string | null; url: string | null };
}

/** /api/v1/diag/http: what the server saw of one HTTPS request, and its clock. */
export interface DiagHttp {
  client: { ip: string; port: number | null; family: "IPv4" | "IPv6" | null };
  server: { ip: string | null; port: number | null; family: "IPv4" | "IPv6" | null };
  http_version: string | null;
  tls: { protocol: string; cipher: string } | null;
  tcp_rtt_ms: number | null;
  tcp_rttvar_ms: number | null;
  time_ms: number;
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
  client_local_ip: string | null;
  client_mac: string | null;
  client_mac_source: "nic" | "eui64" | null;
  server_id: string | null;
  source: string | null;
  download_mbps: number | null;
  upload_mbps: number | null;
  latency_ms: number | null;
  jitter_ms: number | null;
  loss_pct: number | null;
  traffic_bytes: number | null; // download plus upload, as the client counted them
}

/** GET /api/v1/access: whether this visitor needs a code, and gets the desk's functions. */
export interface AccessStatus {
  required: boolean;
  desk: boolean;
  pass?: { valid: boolean; expires: number | null };
}

/** GET /api/v1/access/code, for the service desk. */
export interface AccessCode {
  code: string;
  interval: string;
  timezone: string;
  changes_at: string | null;
  previous: string | null;
  previous_until: string | null;
}
