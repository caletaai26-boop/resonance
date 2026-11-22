import { useEffect, useState } from "react";
import { Asset, Doc, Section, api, fmtTime } from "../api";
import { Empty, Explain, Panel, StatusChip } from "../components/ui";
import { Transport, Waveform } from "./Timeline";
import { useStudio } from "./context";

function Metric({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: "warn" | "ok" }) {
  return (
    <div className="rounded-lg bg-ink-850 border border-ink-700/70 px-3 py-2.5">
      <div className="text-[11px] text-ivory-mute flex items-center gap-1.5">{label}{hint && <Explain>{hint}</Explain>}</div>
      <div className={`font-display text-xl mt-0.5 ${tone === "warn" ? "text-amber" : tone === "ok" ? "text-sage" : ""}`}>{value}</div>
    </div>
  );
}

export function useSections(): Section[] {
  const s = useStudio();
  const [sections, setSections] = useState<Section[]>([]);
  useEffect(() => { api.get(`/api/projects/${s.project.id}/doc/sections`).then((d: Doc) => setSections(d.current?.data.sections || [])).catch(() => {}); }, [s.project.id, s.version]);
  return sections;
}

export default function OverviewTab() {
  const s = useStudio();
  const f = s.agent("forensics");
  const sep = s.agent("separation");
  const tr = s.agent("transcription");
  const m = f?.output?.data?.metrics;
  const art = f?.output?.data?.artifacts;
  const sections = useSections();
  const [stems, setStems] = useState<Asset[]>([]);
  useEffect(() => { api.get(`/api/projects/${s.project.id}/assets?kind=stem`).then(setStems).catch(() => {}); }, [s.project.id, s.version]);
  const duration = m?.duration_s || s.player.duration;
  const analysed = !!f?.output;

  return (
    <div className="space-y-4">
      <Panel title="Original recording" action={
        <button className="btn-primary btn-sm" disabled={s.working} onClick={() => s.runPipeline("analyse")}>{s.working ? "Agents working…" : analysed ? "Re-run analysis" : "Start analysis"}</button>}>
        <Waveform peaksUrl={art ? `/api/assets/${art.waveform}/file` : null} duration={duration} player={s.player} sections={sections} />
        {art && <img src={`/api/assets/${art.spectrogram}/file`} alt="Spectrogram of the recording: time left to right, pitch bottom to top, brightness is loudness" className="w-full h-24 object-fill rounded-lg mt-2 border border-ink-700/70 opacity-90" />}
        <div className="mt-3"><Transport player={s.player} /></div>
        {!analysed && !s.working && <p className="note mt-3">You can play the original now. Start the analysis to measure its quality, find its tempo, key, notes and sections. On a three-minute recording this takes a few minutes.</p>}
      </Panel>

      {!analysed && s.working && <Empty title="Analysing your recording">The agents on the left show live progress. Findings appear here as each one finishes.</Empty>}

      {m && (
        <Panel title="Technical report" action={<StatusChip status={f!.output!.stale ? "stale" : "succeeded"} label="Measured" />}>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
            <Metric label="Length" value={fmtTime(m.duration_s)} />
            <Metric label="Loudness" value={m.integrated_lufs != null ? `${m.integrated_lufs} LUFS` : "n/a"} hint="Average perceived loudness over the whole recording. Streaming services play at about −14; a natural choir recording sits around −20 to −23." />
            <Metric label="Peak" value={`${m.true_peak_db} dBTP`} hint="The highest instantaneous level. 0 is the maximum a digital file can hold." tone={m.true_peak_db > -0.3 ? "warn" : undefined} />
            <Metric label="Clipped passages" value={String(m.clipping.events)} hint="Moments where the phone's microphone overloaded and the waveform was cut flat. This distortion cannot be fully repaired." tone={m.clipping.events ? "warn" : "ok"} />
            <Metric label="Background noise (est.)" value={`${m.noise.snr_estimate_db} dB below music`} hint="The gap between the music and the quietest moments. Under 25 dB means room noise and talking are clearly audible. This is an estimate: the music rarely stops completely." tone={m.noise.snr_estimate_db < 25 ? "warn" : undefined} />
            <Metric label="Level variation" value={`${m.loudness_profile.range_db} dB`} hint="How much the volume swings between loud and quiet passages." />
            <Metric label="Highest frequency" value={`${(m.spectrum.rolloff_99_hz / 1000).toFixed(1)} kHz`} hint="Where the recording's content effectively stops. Human hearing reaches about 16–20 kHz; phone recordings often stop far lower, which sounds dull." tone={m.spectrum.rolloff_99_hz < 10000 ? "warn" : undefined} />
            <Metric label="Stereo" value={!m.stereo.is_stereo || m.stereo.effectively_mono ? "Mono" : "Stereo"} />
          </div>
          <div className="grid md:grid-cols-2 gap-4 mt-4">
            <div>
              <div className="panel-title mb-2">Observations</div>
              <ul className="space-y-1.5">
                {f!.output!.data.observations.map((o: any, i: number) => (
                  <li key={i} className="flex gap-2 text-[13px] leading-relaxed">
                    <span className={o.severity === "warning" ? "text-amber" : "text-ivory-mute"}>{o.severity === "warning" ? "▲" : "·"}</span>
                    <span className="text-ivory-dim">{o.text}{o.time != null && <button className="ml-1.5 text-copper-soft hover:underline font-mono text-[12px]" onClick={() => { s.player.seek(o.time); s.player.play(); }}>{fmtTime(o.time)}</button>}</span>
                  </li>
                ))}
              </ul>
            </div>
            <div>
              <div className="panel-title mb-2">Recommended clean-up</div>
              {f!.output!.data.recommendations.length === 0 ? <p className="note">None needed.</p> : (
                <ul className="space-y-1.5">{f!.output!.data.recommendations.map((r: any, i: number) => <li key={i} className="text-[13px] text-ivory-dim"><span className="text-ivory">{({ denoise: "Noise reduction", tonal_eq: "Tone correction", level_balance: "Level balancing", dc_remove: "DC removal" } as any)[r.op] || r.op}:</span> {r.reason}</li>)}</ul>
              )}
              <p className="note mt-3">{f!.output!.data.uncertainty}</p>
            </div>
          </div>
        </Panel>
      )}

      {tr?.output && (
        <Panel title="Musical findings (estimates, not yet verified)">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
            <Metric label="Tempo" value={`${Math.round(tr.output.data.tempo.tempo_bpm)} BPM`} hint={`Beat tracking can lock onto a multiple of the real pulse. Other plausible readings: ${(tr.output.data.tempo.tempo_alternatives || []).join(", ")} BPM.`} />
            <Metric label="Key" value={tr.output.data.key.key} hint={`Confidence ${Math.round(tr.output.data.key.confidence * 100)}%. Alternatives: ${tr.output.data.key.alternatives.map((a: any) => a.key).join(", ")}.`} />
            <Metric label="Metre" value={`${tr.output.data.tempo.time_signature[0]}/${tr.output.data.tempo.time_signature[1]}`} hint={`Confidence ${Math.round(tr.output.data.tempo.meter_confidence * 100)}%. The detector only distinguishes three beats from four.`} />
            <Metric label="Notes found" value={String(tr.output.data.polyphonic_notes || tr.output.data.melody_notes)} hint={`${tr.output.data.low_confidence_notes} are flagged uncertain. Review them in the Score tab.`} />
          </div>
          <p className="note mt-3">{tr.output.data.uncertainty}</p>
        </Panel>
      )}

      {(sep?.output || sep?.status === "blocked") && (
        <Panel title="Separated stems" action={sep.status === "blocked" ? <StatusChip status="blocked" label="Engine needs setup" /> : undefined}>
          {sep.status === "blocked" && <div className="callout">{sep.job?.error}</div>}
          {stems.length > 0 && (
            <div className="space-y-3">
              {(sep.output?.data.stems || []).map((st: any) => (
                <div key={st.asset_id} className="grid md:grid-cols-[1fr_300px] gap-3 items-center">
                  <div><div className="text-sm">{st.label} <span className="text-ivory-mute text-[12px]">· {Math.round(st.energy_share * 100)}% of energy</span></div><p className="note">{st.warning}</p></div>
                  <audio controls preload="none" src={`/api/assets/${st.asset_id}/file`} className="w-full h-9" onPlay={() => s.player.pause()} />
                </div>
              ))}
              <p className="note">Compare each stem against the original above. {(sep.output?.data.limitations || [])[2]}</p>
            </div>
          )}
        </Panel>
      )}
    </div>
  );
}
