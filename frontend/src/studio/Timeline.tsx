import { useEffect, useMemo, useRef, useState } from "react";
import { Section, api, fmtTime } from "../api";
import { Player } from "./context";

const KIND_COLOR: Record<string, string> = { vocal: "#c98a5a", instrumental: "#6f86b8", unknown: "#7d8496" };

/** Canvas waveform drawn from real peak data computed by the forensics agent, with section overlay,
 *  playhead, click-to-seek and shift-drag (or drag) loop selection. */
export function Waveform({ peaksUrl, duration, player, sections = [], height = 132, colour = "#c98a5a", label, timeMap, onSectionClick }: {
  peaksUrl: string | null; duration: number; player: Player; sections?: Section[]; height?: number; colour?: string; label?: string;
  timeMap?: (t: number) => number; onSectionClick?: (i: number) => void;
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [peaks, setPeaks] = useState<number[] | null>(null);
  const [width, setWidth] = useState(800);
  const drag = useRef<{ x0: number; moved: boolean } | null>(null);
  const [sel, setSel] = useState<[number, number] | null>(null);

  useEffect(() => { setPeaks(null); if (peaksUrl) api.get(peaksUrl).then((d) => setPeaks(d.peaks)).catch(() => setPeaks([])); }, [peaksUrl]);
  useEffect(() => {
    if (!wrap.current) return;
    const ro = new ResizeObserver((e) => setWidth(Math.max(200, Math.floor(e[0].contentRect.width))));
    ro.observe(wrap.current);
    return () => ro.disconnect();
  }, []);

  const shown = timeMap ? timeMap(player.time) : player.time;
  const x = (t: number) => (duration > 0 ? (t / duration) * width : 0);
  const loop = sel || player.loop;

  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    const dpr = window.devicePixelRatio || 1;
    c.width = width * dpr; c.height = height * dpr;
    const g = c.getContext("2d")!;
    g.scale(dpr, dpr);
    g.clearRect(0, 0, width, height);
    const mid = height / 2;
    if (!peaks || peaks.length === 0) {
      g.strokeStyle = "#2e3548"; g.beginPath(); g.moveTo(0, mid); g.lineTo(width, mid); g.stroke();
      return;
    }
    const max = Math.max(...peaks, 0.01);
    const bar = 2, gap = 1, n = Math.floor(width / (bar + gap));
    const px = x(shown);
    for (let i = 0; i < n; i++) {
      const a = Math.floor((i / n) * peaks.length), b = Math.max(a + 1, Math.floor(((i + 1) / n) * peaks.length));
      let v = 0;
      for (let k = a; k < b; k++) v = Math.max(v, peaks[k]);
      const h = Math.max(1.5, (v / max) * (height - 14));
      const bx = i * (bar + gap);
      g.fillStyle = bx < px ? colour : "#3a4259";
      g.globalAlpha = bx < px ? 1 : 0.9;
      g.beginPath();
      (g as any).roundRect ? (g as any).roundRect(bx, mid - h / 2, bar, h, 1) : g.rect(bx, mid - h / 2, bar, h);
      g.fill();
    }
    g.globalAlpha = 1;
  }, [peaks, width, height, shown, duration, colour]);

  const tAt = (clientX: number) => { const r = wrap.current!.getBoundingClientRect(); return Math.max(0, Math.min(duration, ((clientX - r.left) / r.width) * duration)); };
  const inverse = (t: number) => t; // seeking is done in this track's own time; caller maps if needed

  return (
    <div className="select-none">
      {label && <div className="flex justify-between text-[11px] uppercase tracking-wider text-ivory-mute mb-1"><span>{label}</span><span className="font-mono normal-case tracking-normal">{fmtTime(shown)} / {fmtTime(duration)}</span></div>}
      <div ref={wrap} className="relative rounded-lg bg-ink-950/70 border border-ink-700/70 overflow-hidden cursor-crosshair" style={{ height }}
        onMouseDown={(e) => { drag.current = { x0: e.clientX, moved: false }; }}
        onMouseMove={(e) => { const d = drag.current; if (!d) return; if (Math.abs(e.clientX - d.x0) > 4) { d.moved = true; const a = tAt(d.x0), b = tAt(e.clientX); setSel([Math.min(a, b), Math.max(a, b)]); } }}
        onMouseUp={(e) => {
          const d = drag.current; drag.current = null;
          if (d?.moved && sel && sel[1] - sel[0] > 0.3) { player.setLoop(sel); player.seek(sel[0]); }
          else { player.seek(inverse(tAt(e.clientX))); }
          setSel(null);
        }}
        onMouseLeave={() => { drag.current = null; setSel(null); }}
        role="slider" aria-label={`${label || "Recording"} position`} aria-valuemin={0} aria-valuemax={Math.round(duration)} aria-valuenow={Math.round(shown)} tabIndex={0}
        onKeyDown={(e) => { if (e.key === "ArrowRight") player.seek(player.time + 2); if (e.key === "ArrowLeft") player.seek(player.time - 2); if (e.key === " ") { e.preventDefault(); player.toggle(); } }}>
        {sections.map((s, i) => (
          <div key={i} className="absolute top-0 bottom-0 border-l border-ivory/15" style={{ left: x(s.start), width: Math.max(1, x(s.end) - x(s.start)), background: `${KIND_COLOR[s.kind] || KIND_COLOR.unknown}14` }} />
        ))}
        {loop && <div className="absolute top-0 bottom-0 bg-champagne/15 border-x border-champagne/70 pointer-events-none" style={{ left: x(loop[0]), width: x(loop[1]) - x(loop[0]) }} />}
        <canvas ref={canvas} style={{ width, height }} className="absolute inset-0 pointer-events-none" />
        <div className="absolute top-0 bottom-0 w-px bg-ivory pointer-events-none" style={{ left: x(shown) }} />
      </div>
      {sections.length > 0 && (
        <div className="relative h-7 mt-1">
          {sections.map((s, i) => (
            <button key={i} title={`${s.label} · ${fmtTime(s.start)}–${fmtTime(s.end)}${s.status === "verified" ? " · verified" : " · detected, not verified"}`}
              onClick={() => { player.seek(s.start); onSectionClick?.(i); }}
              className="absolute top-0 h-6 px-1.5 text-[11px] text-left truncate rounded-sm border-l-2 hover:brightness-125 transition"
              style={{ left: x(s.start), width: Math.max(4, x(s.end) - x(s.start) - 2), borderColor: KIND_COLOR[s.kind] || KIND_COLOR.unknown, background: `${KIND_COLOR[s.kind] || KIND_COLOR.unknown}26`,
                borderStyle: s.status === "verified" ? "solid" : "dashed" }}>
              {s.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function Transport({ player, extra }: { player: Player; extra?: React.ReactNode }) {
  const rates = useMemo(() => [0.5, 0.75, 1, 1.25], []);
  return (
    <div className="flex flex-wrap items-center gap-3">
      <button className="btn-primary !rounded-full w-10 h-10 !p0 !px-0" onClick={player.toggle} aria-label={player.playing ? "Pause" : "Play"} disabled={!player.ready}>
        {player.playing ? <svg width="14" height="14" viewBox="0 0 14 14"><rect x="2" y="1" width="3.5" height="12" rx="1" fill="currentColor" /><rect x="8.5" y="1" width="3.5" height="12" rx="1" fill="currentColor" /></svg>
          : <svg width="14" height="14" viewBox="0 0 14 14"><path d="M3 1.5v11l9-5.5z" fill="currentColor" /></svg>}
      </button>
      <span className="font-mono text-sm tabular-nums text-ivory-dim whitespace-nowrap">{fmtTime(player.time)} / {fmtTime(player.duration)}</span>
      <div className="flex items-center gap-1" role="group" aria-label="Playback speed">
        {rates.map((r) => <button key={r} className={`btn-sm btn ${player.rate === r ? "!border-copper/70 !text-copper-soft" : ""}`} onClick={() => player.setRate(r)}>{r}×</button>)}
      </div>
      {player.loop ? <button className="btn btn-sm !border-champagne/60 !text-champagne" onClick={() => player.setLoop(null)}>Looping {fmtTime(player.loop[0])}–{fmtTime(player.loop[1])} ✕</button>
        : <span className="text-[12px] text-ivory-mute">Drag across the waveform to loop a passage</span>}
      {extra}
    </div>
  );
}
