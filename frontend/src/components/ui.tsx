import { ReactNode, useEffect, useState } from "react";

export function Logo({ size = 22 }: { size?: number }) {
  return (
    <span className="inline-flex items-center gap-2.5 select-none">
      <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden>
        <rect width="32" height="32" rx="8" fill="#131722" stroke="#2e3548" />
        <path d="M5 16h3l2-7 3 14 3-18 3 16 2-8 2 3h4" fill="none" stroke="#c98a5a" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <span className="font-display text-[17px] tracking-[0.22em] text-ivory">RESONANCE</span>
    </span>
  );
}

const STATUS: Record<string, [string, string]> = {
  succeeded: ["chip-ok", "Done"], approved: ["chip-ok", "Approved"], ready: ["chip-ok", "Ready"], pass: ["chip-ok", "Pass"], final: ["chip-ok", "Final"], verified: ["chip-ok", "Verified"],
  running: ["chip-live", "Working"], queued: ["chip-live", "Queued"],
  blocked: ["chip-warn", "Blocked"], warn: ["chip-warn", "Attention"], invalidated: ["chip-warn", "Needs re-approval"], stale: ["chip-warn", "Out of date"],
  needs_setup: ["chip-warn", "Needs setup"], draft: ["chip-warn", "Draft"], detected: ["chip-warn", "Detected"], unverified: ["chip-warn", "Unverified"],
  failed: ["chip-bad", "Failed"], rejected: ["chip-bad", "Rejected"], error: ["chip-bad", "Error"],
  idle: ["chip-idle", "Idle"], pending: ["chip-idle", "Pending"], cancelled: ["chip-idle", "Cancelled"], info: ["chip-idle", "Info"], not_evaluated: ["chip-idle", "Not evaluated"],
};

export function StatusChip({ status, label }: { status: string; label?: string }) {
  const [cls, text] = STATUS[status] || ["chip-idle", status];
  return (
    <span className={cls}>
      {(status === "running" || status === "queued") && <span className="live-dot !w-1.5 !h-1.5" />}
      {label || text}
    </span>
  );
}

export function Panel({ title, action, children, className = "" }: { title?: ReactNode; action?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`panel ${className}`}>
      {(title || action) && (
        <header className="flex items-center justify-between gap-3 px-4 pt-3.5 pb-2">
          <h3 className="panel-title">{title}</h3>
          {action}
        </header>
      )}
      <div className="px-4 pb-4">{children}</div>
    </section>
  );
}

export function ErrorNote({ error, onClose }: { error: string | null; onClose?: () => void }) {
  if (!error) return null;
  return (
    <div role="alert" className="rounded-lg border border-rose/40 bg-rose/10 px-3 py-2 text-[13px] text-rose flex items-start justify-between gap-3 rise">
      <span>{error}</span>
      {onClose && <button className="text-rose/70 hover:text-rose" onClick={onClose} aria-label="Dismiss">✕</button>}
    </div>
  );
}

export function Modal({ title, children, onClose, wide }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 backdrop-blur-sm p-4" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div role="dialog" aria-modal="true" aria-label={title} className={`panel w-full ${wide ? "max-w-2xl" : "max-w-lg"} rise max-h-[90vh] overflow-auto`}>
        <header className="flex items-center justify-between px-5 pt-4 pb-3 border-b border-ink-700/70">
          <h2 className="h-display text-xl">{title}</h2>
          <button className="btn-ghost btn-sm" onClick={onClose} aria-label="Close">✕</button>
        </header>
        <div className="p-5">{children}</div>
      </div>
    </div>
  );
}

export function Explain({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="relative inline-block align-middle">
      <button type="button" aria-label="Explain" onClick={() => setOpen((o) => !o)} onBlur={() => setOpen(false)}
        className="w-4 h-4 rounded-full border border-ink-500 text-[10px] leading-none text-ivory-mute hover:text-ivory hover:border-copper transition">?</button>
      {open && <span className="absolute z-30 left-1/2 -translate-x-1/2 top-6 w-64 panel p-3 text-[12px] leading-relaxed text-ivory-dim normal-case tracking-normal font-normal rise">{children}</span>}
    </span>
  );
}

export function Meter({ value, max = 1, tone = "copper" }: { value: number; max?: number; tone?: "copper" | "sage" | "rose" }) {
  const pct = Math.max(0, Math.min(1, value / max)) * 100;
  const bg = tone === "sage" ? "#8fb69a" : tone === "rose" ? "#d98880" : "#c98a5a";
  return <span className="inline-block w-16 h-1.5 rounded-full bg-ink-700 align-middle overflow-hidden"><span className="block h-full rounded-full" style={{ width: `${pct}%`, background: bg }} /></span>;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-ink-600 px-6 py-10 text-center">
      <div className="h-display text-lg mb-1.5">{title}</div>
      <div className="note max-w-md mx-auto">{children}</div>
    </div>
  );
}

export function useAsync<T>(fn: () => Promise<T>, deps: any[]): { data: T | null; error: string | null; loading: boolean; reload: () => void; setData: (d: T) => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    setLoading(true);
    fn().then((d) => { if (live) { setData(d); setError(null); } }).catch((e) => live && setError(e.message)).finally(() => live && setLoading(false));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return { data, error, loading, reload: () => setTick((t) => t + 1), setData };
}
