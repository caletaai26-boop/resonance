import { useState } from "react";
import { api } from "../api";
import { ErrorNote, Panel, StatusChip, useAsync } from "../components/ui";

export default function Settings() {
  const caps = useAsync<any>(() => api.get("/api/capabilities"), []);
  const prov = useAsync<any>(() => api.get("/api/settings/providers"), []);
  const [key, setKey] = useState("");
  const [model, setModel] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; detail: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function save() {
    setBusy(true); setError(null); setMsg(null);
    try {
      const body: any = {};
      if (key) body.anthropic_api_key = key;
      if (model) body.anthropic_model = model;
      prov.setData(await api.put("/api/settings/providers", body));
      setKey("");
      setMsg({ ok: true, detail: "Saved on the server. The key is never sent back to the browser." });
    } catch (e: any) { setError(e.message); } finally { setBusy(false); }
  }
  async function test() {
    setBusy(true); setMsg(null);
    try { setMsg(await api.post("/api/settings/providers/anthropic/test")); } catch (e: any) { setError(e.message); } finally { setBusy(false); }
  }
  async function clear() {
    prov.setData(await api.put("/api/settings/providers", { anthropic_api_key: "" }));
    setMsg({ ok: true, detail: "Key removed." });
  }

  const engines: any[] = caps.data?.engines || [];
  return (
    <div className="space-y-8">
      <div>
        <h1 className="h-display text-3xl">Engines &amp; settings</h1>
        <p className="note mt-1 max-w-2xl">What this installation can genuinely do today, and what each missing capability needs. Nothing listed as “needs setup” is simulated anywhere in the studio.</p>
      </div>
      <ErrorNote error={caps.error || prov.error || error} />
      <Panel title="Audio and music engines">
        <div className="divide-y divide-ink-700/60">
          {engines.map((e) => (
            <div key={e.name} className="py-3 grid md:grid-cols-[190px_1fr_auto] gap-x-5 gap-y-1 items-start">
              <div><div className="text-[11px] uppercase tracking-wider text-ivory-mute">{e.area}</div><div className="text-sm mt-0.5">{e.name}</div></div>
              <div className="note">
                {e.detail}
                {e.status !== "ready" && e.needs && <div className="mt-1"><span className="text-ivory">To enable: </span>{e.needs}</div>}
                {e.cost && <div className="mt-1 text-ivory-mute">Cost: {e.cost}</div>}
              </div>
              <StatusChip status={e.status} />
            </div>
          ))}
        </div>
      </Panel>
      <div className="grid lg:grid-cols-2 gap-4">
        <Panel title="Language model (optional)">
          <p className="note mb-3">{prov.data?.anthropic.used_for}</p>
          <div className="space-y-3">
            <div className="flex items-center gap-2 text-sm">Anthropic API <StatusChip status={prov.data?.anthropic.configured ? "ready" : "needs_setup"} label={prov.data?.anthropic.configured ? `Key saved ${prov.data.anthropic.api_key_masked}` : "No key"} /></div>
            <div><label className="label" htmlFor="k">API key</label><input id="k" type="password" className="input font-mono" placeholder="sk-ant-…" value={key} onChange={(e) => setKey(e.target.value)} autoComplete="off" /></div>
            <div><label className="label" htmlFor="mo">Model</label><input id="mo" className="input font-mono" placeholder={prov.data?.anthropic.model} value={model} onChange={(e) => setModel(e.target.value)} /></div>
            <div className="flex flex-wrap gap-2">
              <button className="btn-primary" disabled={busy || (!key && !model)} onClick={save}>Save</button>
              <button className="btn" disabled={busy || !prov.data?.anthropic.configured} onClick={test}>Test connection</button>
              {prov.data?.anthropic.configured && <button className="btn-ghost" onClick={clear}>Remove key</button>}
            </div>
            {msg && <div className={`text-[13px] ${msg.ok ? "text-sage" : "text-rose"}`}>{msg.detail}</div>}
          </div>
        </Panel>
        <Panel title="Privacy and data handling">
          <ul className="note space-y-2 list-disc pl-4">
            <li>Projects are private to the account that created them. Other accounts cannot see that they exist.</li>
            <li>All audio analysis, separation, transcription and rendering run on this server. No audio leaves it.</li>
            <li>The optional language model receives only numbers and text findings, and only for projects where you switch “external AI” on.</li>
            <li>Original recordings are stored unmodified with a checksum; every derived file records how it was made.</li>
            <li>Deleting a project removes its database records and every stored file. A per-project retention period can delete it automatically.</li>
            <li>Uploaded recordings are never used to train any model.</li>
          </ul>
          <p className="note mt-3 pt-3 border-t border-ink-700/60">Upload limits: {caps.data?.limits.max_upload_mb} MB per file. Audio: {caps.data?.limits.audio_types.join(", ")}. Scores: {caps.data?.limits.score_types.join(", ")}.</p>
        </Panel>
      </div>
    </div>
  );
}
