import { useEffect, useMemo, useRef, useState } from "react";
import { Note, Score, Section, pitchName } from "../api";
import { Player } from "./context";

export const PART_COLOURS = ["#e0ab82", "#7fa7d9", "#9fcbaa", "#d9a0c8", "#d8c56f", "#8fd0d0", "#c9a08a", "#a9a2e0"];
const ROW = 9;
const BLACK = new Set([1, 3, 6, 8, 10]);

let ctx: AudioContext | null = null;
export function beep(pitch: number, dur = 0.35) {
  try {
    ctx = ctx || new AudioContext();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = "triangle";
    o.frequency.value = 440 * Math.pow(2, (pitch - 69) / 12);
    g.gain.setValueAtTime(0.0001, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.18, ctx.currentTime + 0.015);
    g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + dur);
    o.connect(g).connect(ctx.destination);
    o.start(); o.stop(ctx.currentTime + dur + 0.02);
  } catch { /* audio not available */ }
}

type DragState = { id: string; partId: string; mode: "move" | "resize"; x0: number; y0: number; orig: Note; moved: boolean };

/** Editable piano roll. Time runs left to right on the recording's own clock; pitch runs bottom to top.
 *  Drag a note to move it, drag its right edge to change its length, double-click empty space to add one. */
export function PianoRoll({ score, sections, player, hidden, activePart, selected, onSelect, onChange, onAdd, pps, height = 420 }: {
  score: Score; sections: Section[]; player: Player; hidden: Set<string>; activePart: string | null; selected: Set<string>;
  onSelect: (ids: Set<string>, partId?: string) => void; onChange: (partId: string, note: Note, commit: boolean) => void; pps: number; height?: number;
  onAdd?: (partId: string, start: number, pitch: number) => void;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const drag = useRef<DragState | null>(null);
  const [, force] = useState(0);
  const all = score.parts.flatMap((p) => p.notes);
  const [lo, hi] = useMemo(() => {
    if (!all.length) return [48, 84];
    const ps = all.map((n) => n.pitch);
    return [Math.max(0, Math.min(...ps) - 4), Math.min(127, Math.max(...ps) + 4)];
  }, [score]);
  const duration = Math.max(score.duration || 0, player.duration || 0, 10);
  const W = Math.ceil(duration * pps), H = (hi - lo + 1) * ROW;
  const y = (p: number) => (hi - p) * ROW;
  const tAt = (clientX: number) => { const r = scroller.current!.getBoundingClientRect(); return (clientX - r.left + scroller.current!.scrollLeft - 44) / pps; };
  const pAt = (clientY: number) => { const r = scroller.current!.getBoundingClientRect(); return Math.round(hi - (clientY - r.top + scroller.current!.scrollTop - 18 - ROW / 2) / ROW); };

  // keep the playhead in view while playing
  useEffect(() => {
    const el = scroller.current;
    if (!el || !player.playing) return;
    const px = player.time * pps + 44;
    if (px < el.scrollLeft + 60 || px > el.scrollLeft + el.clientWidth - 80) el.scrollLeft = Math.max(0, px - 120);
  }, [player.time, player.playing, pps]);

  useEffect(() => {
    const move = (e: MouseEvent) => {
      const d = drag.current;
      if (!d) return;
      const dx = (e.clientX - d.x0) / pps, dy = Math.round((d.y0 - e.clientY) / ROW);
      if (Math.abs(e.clientX - d.x0) > 2 || dy !== 0) d.moved = true;
      if (!d.moved) return;
      const len = d.orig.end - d.orig.start;
      const start = Math.max(0, d.orig.start + dx);
      const n = d.mode === "move"
        ? { ...d.orig, start, end: start + len, pitch: Math.max(0, Math.min(127, d.orig.pitch + dy)) }
        : { ...d.orig, end: Math.max(d.orig.start + 0.05, d.orig.end + dx) };
      onChange(d.partId, n, false);
      force((v) => v + 1);
    };
    const up = () => {
      const d = drag.current;
      if (d?.moved) {
        const part = score.parts.find((p) => p.id === d.partId);
        const n = part?.notes.find((x) => x.id === d.id);
        if (n) { onChange(d.partId, { ...n, verified: true, uncertain: false }, true); if (n.pitch !== d.orig.pitch) beep(n.pitch); }
      }
      drag.current = null;
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => { window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
  }, [score, pps, onChange]);

  return (
    <div ref={scroller} className="relative overflow-auto rounded-lg border border-ink-700/70 bg-ink-950/70" style={{ height }} tabIndex={0} aria-label="Piano roll editor">
      <svg width={W + 44} height={H + 18} className="block select-none">
        <g transform="translate(44,18)">
          {Array.from({ length: hi - lo + 1 }).map((_, i) => {
            const p = hi - i;
            return <rect key={p} x={0} y={i * ROW} width={W} height={ROW} fill={BLACK.has(p % 12) ? "#10131b" : "#141824"} stroke={p % 12 === 0 ? "#2a3144" : "none"} strokeWidth={p % 12 === 0 ? 0.5 : 0} />;
          })}
          {score.beats.map((b, i) => <line key={i} x1={b * pps} x2={b * pps} y1={0} y2={H} stroke={(i - score.downbeat_offset) % score.time_signature[0] === 0 ? "#333b51" : "#1e2433"} strokeWidth={1} />)}
          {sections.map((s, i) => <g key={i}><line x1={s.start * pps} x2={s.start * pps} y1={-18} y2={H} stroke="#c98a5a" strokeOpacity={0.6} strokeDasharray="3 3" />
            <text x={s.start * pps + 4} y={-6} fontSize={10} fill="#c9c1b0">{s.label}</text></g>)}
          <rect x={0} y={0} width={W} height={H} fill="transparent"
            onClick={(e) => { player.seek(tAt(e.clientX)); onSelect(new Set()); }}
            onDoubleClick={(e) => { if (activePart && onAdd) onAdd(activePart, Math.max(0, tAt(e.clientX)), pAt(e.clientY)); }} />
          {score.parts.map((part, pi) => hidden.has(part.id) ? null : part.notes.map((n) => {
            const col = PART_COLOURS[pi % PART_COLOURS.length];
            const sel = selected.has(n.id);
            const w = Math.max(3, (n.end - n.start) * pps);
            const dim = activePart && activePart !== part.id;
            return (
              <g key={n.id} transform={`translate(${n.start * pps},${y(n.pitch)})`} opacity={dim ? 0.45 : 1}>
                <rect width={w} height={ROW - 1} rx={2} fill={n.uncertain && !n.verified ? "transparent" : col} fillOpacity={n.verified ? 1 : 0.8}
                  stroke={sel ? "#ffffff" : col} strokeWidth={sel ? 1.5 : 1} strokeDasharray={n.uncertain && !n.verified ? "3 2" : undefined} style={{ cursor: "grab" }}
                  onMouseDown={(e) => { e.stopPropagation(); const ids = e.shiftKey ? new Set(selected).add(n.id) : new Set([n.id]); onSelect(ids, part.id); if (!e.shiftKey) beep(n.pitch, 0.25);
                    drag.current = { id: n.id, partId: part.id, mode: "move", x0: e.clientX, y0: e.clientY, orig: { ...n }, moved: false }; }}>
                  <title>{`${pitchName(n.pitch)} · ${n.start.toFixed(2)}–${n.end.toFixed(2)}s · ${part.name}${n.confidence != null ? ` · confidence ${Math.round(n.confidence * 100)}%` : ""}${n.verified ? " · verified" : n.uncertain ? " · uncertain" : ""}${n.lyric ? ` · “${n.lyric}”` : ""}`}</title>
                </rect>
                <rect x={Math.max(0, w - 5)} width={5} height={ROW - 1} fill="transparent" style={{ cursor: "ew-resize" }}
                  onMouseDown={(e) => { e.stopPropagation(); onSelect(new Set([n.id]), part.id); drag.current = { id: n.id, partId: part.id, mode: "resize", x0: e.clientX, y0: e.clientY, orig: { ...n }, moved: false }; }} />
                {n.lyric && w > 14 && <text x={2} y={-2} fontSize={9} fill="#f1e9d8" pointerEvents="none">{n.lyric}</text>}
              </g>
            );
          }))}
          <line x1={player.time * pps} x2={player.time * pps} y1={-18} y2={H} stroke="#f1e9d8" strokeWidth={1} pointerEvents="none" />
        </g>
        <g transform="translate(0,18)">
          <rect x={0} y={-18} width={44} height={H + 18} fill="#0e1118" />
          {Array.from({ length: hi - lo + 1 }).map((_, i) => { const p = hi - i; return p % 12 === 0 || p % 12 === 7 ? <text key={p} x={38} y={i * ROW + ROW - 1.5} fontSize={9} textAnchor="end" fill={p % 12 === 0 ? "#c9c1b0" : "#6d7286"}>{pitchName(p)}</text> : null; })}
        </g>
      </svg>
    </div>
  );
}

