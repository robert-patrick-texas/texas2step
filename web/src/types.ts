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
  /** Idle latency only: how long a new connection took to open (TCP, TLS and a first
   * request), with nothing else running. */
  setup_ms?: number;
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
  /** The responsiveness test: new connections opened during the transfer, and round trips
   * on the loaded connection itself (HTTP/2 or 3 only). */
  connections?: NewConnections;
  loaded_probes?: number[];
}

/** New connections opened while a transfer ran: each one's open time (TCP connect, TLS
 * handshake and first request), or each step's from the command line, which can time them
 * apart. `tls` says whether there was a TLS handshake to time. */
export interface NewConnections {
  setup_ms: number[];
  tcp_ms?: number[];
  tls_ms?: number[];
  http_ms?: number[];
  lost: number;
  tls: boolean;
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
  /** With the server's stamps: each probe's delay above the fastest, each way (null when
   * lost), and the probes lost on the way to the server (the rest were lost coming back). */
  up_ms?: (number | null)[];
  down_ms?: (number | null)[];
  lost_upstream?: number[];
  note?: string;
  error?: string;
}

export type CallRating = "excellent" | "good" | "fair" | "poor" | "bad";

/** A call over one direction (or both together, from round trips alone). */
export interface CallSide {
  packets: number; // in the call's view: the probes a call's 20 ms packets would be
  late: number; // probes more than the jitter buffer behind the fastest
  late_pct: number;
  impaired_pct: number; // lost or late, of every probe
  burst_ratio: number | null; // 1 for random loss, more for bursts; null without any
  longest_gap_ms: number; // the longest run of lost or late packets in the call's view
  r: number; // the E-model's rating
  mos: number;
  mos_rating: CallRating; // the E-model's, from the score alone
  rating: CallRating; // as a caller would hear it: mos_rating, at most what the longest gap allows
}

/** A call's quality over the path the packet-loss test measured (src/texas2step/quality.py).
 * With the server's stamps, `down` is what you hear and `up` what the other end hears, and
 * the figures at the top are the worse one's. */
export interface CallQuality extends CallSide {
  basis: "round trip" | "each way";
  interval_ms: number; // the call's packet spacing
  delay_ms: number; // mouth to ear: one way, plus what the call adds
  worse?: "up" | "down";
  up?: CallSide;
  down?: CallSide;
}

/** One direction's responsiveness by the IETF draft's method. */
export interface ResponsivenessSide {
  rpm: number;
  foreign_rpm: number; // from the new connections
  loaded_rpm: number | null; // from round trips on the loaded connection, or beside it
  setup_ms: number; // a new connection's open time: the fastest 95 %'s mean
  loaded_ms: number | null;
  loaded_on: "loaded connection" | "own connection";
}

/** Round trips per minute under load: each direction's, and the lower of the two. With
 * basis "ippm" each direction has its details, and `idle_setup_ms` is how long a new
 * connection took to open before the load. */
export interface Responsiveness {
  rpm: number;
  download_rpm: number | null;
  upload_rpm: number | null;
  basis: "latency under load" | "ippm";
  download?: ResponsivenessSide;
  upload?: ResponsivenessSide;
  idle_setup_ms?: number;
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
  /** `one_stream`: whether the run measured one stream's download too. */
  config: { streams: number; seconds: number; packets: number; interval_ms: number; one_stream?: boolean };
  latency?: LatencyStats;
  download?: ThroughputResult;
  upload?: ThroughputResult;
  /** After the download, one stream for the same time: what a single file copy gets. */
  one_stream?: ThroughputResult;
  loss?: LossResult;
  bufferbloat?: Bufferbloat;
  call?: CallQuality;
  responsiveness?: Responsiveness;
  readiness?: Readiness[];
}

export interface ServerInfo {
  name: string;
  /** Left out for visitors outside internal_networks (docs/access.md). */
  version?: string;
  server: { id: string; name: string; location: string };
  /** `stamps`: the UDP probe's echoes can carry the server's stamps; older servers leave it out. */
  capabilities: { websocket: boolean; webrtc: boolean; results: boolean; measure: boolean; stamps?: boolean };
  /** `max_session_seconds`: session_max_runtime, 0 for none; older servers leave it out. */
  limits: { max_download_bytes: number; max_upload_bytes: number; max_stream_seconds: number; max_session_seconds?: number };
  ice_servers: RTCIceServer[];
  /** `one_stream`: whether a test measures one stream's download unless the visitor
   * chooses; older servers leave it out. */
  defaults: { streams: number; seconds: number; packets: number; one_stream?: boolean };
  /** With a traffic limit: whether this visitor may start a test; if not, the site's
   * message, and a link to continue elsewhere if it gave one. */
  traffic?: { open: boolean; message: string | null; url: string | null };
  /** Monitor's profile until a visitor picks one, and whether the video one is offered;
   * older servers leave it out. */
  monitor?: { profile: "path" | "voice" | "video"; voice?: boolean; video: boolean };
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
  // Single-family names that each serve one HTTP version, for Diag's rows; "" if not set.
  url_v4_http1: string;
  url_v4_http3: string;
  url_v6_http1: string;
  url_v6_http3: string;
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
  codes: boolean; // this server gives passes, even to a visitor who needs none
  note: string; // the site's line under the code box (access_note); empty for none
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
