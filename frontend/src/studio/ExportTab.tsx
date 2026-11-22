import { useEffect, useState } from "react";
import { Asset, api, fmtBytes, fmtDate } from "../api";
import { Panel, StatusChip } from "../components/ui";
import { useStudio } from "./context";

export default function ExportTab() {
  const s = useStudio();
  const [data, setData] = useState<any>(null);
  const [history, setHistory] = useState<any[]>([]);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [showAssets, setShowAssets] = useState(false);
  const load = () => {
    api.get(`/api/projects/${s.project.id}/exports`).then(setData).catch((e) => s.setError(e.message));
    api.get(`/api/projects/${s.project.id}/history`).then(setHistory).catch(() => {});
    api.get(`/api/projects/${s.project.id}/assets`).then(setAssets).catch(() => {});
  };
  useEffect(load, [s.project.id, s.version]);
  const allApproved = s.status.gates.every((g) => g.state === "approved");
  const engines: any[] = (s.caps?.engines || []).filter((e: any) => e.status !== "ready");
  if (!data) return null;
  return (
    <div className="space-y-4">
      <div className={`rounded-lg border px-3 py-2.5 text-[13px] ${allApproved ? "border-sage/40 bg-sage/5" : "border-amber/40 bg-amber/5"}`}>
        {allApproved ? <><span className="text-sage font-medium">Final master authorised.</span> <span className="text-ivory-dim">Audio exports are labelled FINAL.</span></>
          : <><span className="text-amber font-medium">Draft status.</span> <span className="text-ivory-dim">Audio can be exported now, but files are named DRAFT until every checkpoint on the left is approved. This stops an unverified render from being mistaken for the finished master.</span></>}
      </div>
      <Panel title="Exports">
        <div className="divide-y divide-ink-700/60">
          {data.options.map((o: any) => (
            <div key={o.key} className="py-2.5 flex flex-wrap items-center justify-between gap-3">
              <div className="min-w-0 flex-1"><div className="text-sm">{o.label}</div>
                {o.available ? o.note && <div className="note">{o.note}</div> : <div className="note"><span className="text-amber">Unavailable: </span>{o.reason}</div>}</div>
              {o.available ? <a className="btn btn-sm" href={o.url} onClick={() => setTimeout(load, 1500)}>Download</a> : <StatusChip status="idle" label="Unavailable" />}
            </div>
          ))}
        </div>
        <p className="note mt-3">Every download is generated from the files and versions stored for this project. Nothing here is a placeholder.</p>
      </Panel>
      <Panel title="What still needs additional engines or services">
        <div className="divide-y divide-ink-700/60">
          {engines.map((e) => (
            <div key={e.name} className="py-2.5 grid md:grid-cols-[200px_1fr] gap-x-4 gap-y-1"><div><div className="text-[11px] uppercase tracking-wider text-ivory-mute">{e.area}</div><div className="text-[13px]">{e.name}</div></div>
              <div className="note">{e.detail}{e.needs && <div className="mt-0.5"><span className="text-ivory">To enable: </span>{e.needs}</div>}{e.cost && <div className="text-ivory-mute">Cost: {e.cost}</div>}</div></div>
          ))}
        </div>
      </Panel>
      <div className="grid lg:grid-cols-2 gap-4">
        <Panel title="Project history">
          <ol className="space-y-1.5 max-h-80 overflow-auto">
            {history.map((h) => <li key={h.id} className="text-[13px] flex gap-3"><span className="text-ivory-mute font-mono text-[11px] w-24 shrink-0 pt-0.5">{fmtDate(h.created_at)}</span><span className="text-ivory-dim">{h.message}</span></li>)}
            {history.length === 0 && <li className="note">Nothing yet.</li>}
          </ol>
        </Panel>
        <Panel title="Stored files and how each was made" action={<button className="btn-ghost btn-sm" onClick={() => setShowAssets((v) => !v)}>{showAssets ? "Hide" : `Show ${assets.length}`}</button>}>
          {!showAssets ? <p className="note">Every file records its source recording, the method and engine version that produced it, the settings used, its checksum and its verification status.</p> : (
            <ul className="space-y-2 max-h-80 overflow-auto">
              {assets.map((a) => (
                <li key={a.id} className="text-[12px] border-b border-ink-700/50 pb-2">
                  <div className="flex justify-between gap-2"><span className="text-ivory text-[13px] truncate">{a.label || a.filename}</span><StatusChip status={a.verification === "approved" ? "approved" : a.verification} /></div>
                  <div className="text-ivory-mute">{a.kind} · {fmtBytes(a.size_bytes)} · {a.method}{a.engine && ` · ${a.engine} ${a.engine_version}`}</div>
                  <div className="text-ivory-mute font-mono text-[10px] truncate">sha256 {a.sha256} · {fmtDate(a.created_at)}</div>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </div>
  );
}
