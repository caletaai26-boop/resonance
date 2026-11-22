// Thin typed-ish client for the RESONANCE API. Auth is an HttpOnly cookie, so no tokens live in JS.
export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function parse(res: Response) {
  if (res.ok) return res.status === 204 ? null : res.json();
  let msg = `${res.status} ${res.statusText}`;
  try {
    const j = await res.json();
    if (typeof j.detail === "string") msg = j.detail;
    else if (Array.isArray(j.detail)) msg = j.detail.map((d: any) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
  } catch { /* keep default */ }
  if (res.status === 401 && !location.pathname.startsWith("/login")) location.assign("/login");
  throw new ApiError(res.status, msg);
}

export const api = {
  get: (url: string) => fetch(url, { credentials: "same-origin" }).then(parse),
  send: (method: string, url: string, body?: unknown) =>
    fetch(url, { method, credentials: "same-origin", headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body) }).then(parse),
  post: (url: string, body?: unknown) => api.send("POST", url, body),
  put: (url: string, body?: unknown) => api.send("PUT", url, body),
  patch: (url: string, body?: unknown) => api.send("PATCH", url, body),
  del: (url: string) => api.send("DELETE", url),
  upload(url: string, file: File, fields: Record<string, string> = {}, onProgress?: (f: number) => void): Promise<any> {
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      Object.entries(fields).forEach(([k, v]) => fd.append(k, v));
      fd.append("file", file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url);
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
      xhr.onload = () => {
        let j: any = null;
        try { j = JSON.parse(xhr.responseText); } catch { /* ignore */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(j);
        else reject(new ApiError(xhr.status, (j && typeof j.detail === "string" && j.detail) || `Upload failed (${xhr.status})`));
      };
      xhr.onerror = () => reject(new ApiError(0, "Network error during upload"));
      xhr.send(fd);
    });
  },
};

export type Gate = { gate: number; key: string; title: string; explain: string; state: "pending" | "approved" | "rejected" | "invalidated"; comment: string; decided_at: string | null };
export type Job = { id: string; agent: string; status: string; progress: number; step: string; attempts: number; max_attempts: number; error: string; log: { t: string; msg: string }[]; created_at: string; started_at: string | null; finished_at: string | null };
export type AgentView = { key: string; number: number; name: string; role: string; status: string; job: Job | null; output: { id: string; summary: string; data: any; stale: boolean; created_at: string } | null; next_action: string };
export type Asset = { id: string; kind: string; label: string; filename: string; mime: string; size_bytes: number; sha256: string; method: string; engine: string; engine_version: string; settings: any; meta: any; verification: string; created_at: string; url: string };
export type Status = { stage: string; next_action: string; gates: Gate[]; blockers: { agent: string; status: string; message: string }[]; active_jobs: Job[]; has_source: boolean; mode: string; current: any; summary: string; narration?: any };
export type Project = { id: string; title: string; description: string; mode: "restore" | "faithful" | "creative"; language: string; rights: any; retention_days: number | null; allow_external_ai: boolean; created_at: string; updated_at: string; source: Asset | null; stage: string; next_action: string; pending_gate: number | null; active_jobs: number; blockers: number; status?: Status };
export type Note = { id: string; start: number; end: number; pitch: number; velocity: number; confidence: number | null; lyric: string; uncertain: boolean; verified: boolean };
export type Part = { id: string; name: string; role: "vocal" | "instrument"; voice: string; program: number; source: string; notes: Note[] };
export type Score = { key: string | null; key_confidence: number | null; time_signature: [number, number]; meter_confidence: number | null; tempo_bpm: number | null; tempo_curve: any[]; beats: number[]; downbeat_offset: number; duration: number; parts: Part[]; chords: { start: number; end: number; label: string; confidence: number | null }[]; warnings: string[] };
export type Version<T = any> = { id: string; version: number; parent_id: string | null; origin: string; note: string; provenance: any; verification: string; created_at: string; data: T };
export type Doc<T = any> = { current: Version<T> | null; head_id: string | null; versions: Omit<Version<T>, "data">[]; default?: T };
export type Section = { start: number; end: number; label: string; kind: "vocal" | "instrumental" | "unknown"; group: string; status: "detected" | "verified"; confidence?: number; vocal_ratio?: number | null };

export const fmtTime = (s: number | null | undefined) => {
  if (s == null || !isFinite(s)) return "–";
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(1).padStart(4, "0")}`;
};
export const fmtBytes = (n: number) => (n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} kB`);
export const fmtDate = (iso: string) => new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const NAMES = ["C", "C♯", "D", "E♭", "E", "F", "F♯", "G", "A♭", "A", "B♭", "B"];
export const pitchName = (p: number) => `${NAMES[((p % 12) + 12) % 12]}${Math.floor(p / 12) - 1}`;
export const MODE_LABEL: Record<string, string> = { restore: "Restore Original", faithful: "Faithful Reconstruction", creative: "Creative Reimagining" };
