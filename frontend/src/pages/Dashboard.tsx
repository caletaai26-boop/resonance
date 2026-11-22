import { FormEvent, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { MODE_LABEL, Project, api, fmtDate } from "../api";
import { Empty, ErrorNote, Modal, Panel, StatusChip, useAsync } from "../components/ui";

const MODES = [
  { key: "faithful", title: "Faithful Reconstruction", text: "Replace the singers and instruments with a new performance of the same notes, words and structure. Nothing is added without your approval.", tag: "Recommended" },
  { key: "restore", title: "Restore Original", text: "Keep the actual recorded performance. Reduce noise, correct tone, master. No notes or performers are replaced." },
  { key: "creative", title: "Creative Reimagining", text: "Allow changes to instruments, harmonies and style. Kept strictly separate from faithful work." },
];

function NewProject({ onClose }: { onClose: () => void }) {
  const nav = useNavigate();
  const [title, setTitle] = useState("Adeste Fideles — Christmas Mass, Gibraltar");
  const [description, setDescription] = useState("");
  const [mode, setMode] = useState("faithful");
  const [language, setLanguage] = useState("la");
  const [melody, setMelody] = useState("Traditional (public domain)");
  const [recording, setRecording] = useState("My own phone recording");
  const [ack, setAck] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const p = await api.post("/api/projects", { title, description, mode, language, rights_acknowledged: ack, rights: { melody, recording, arrangement: "Unknown / as performed" } });
      nav(`/p/${p.id}`);
    } catch (err: any) {
      setError(err.message);
      setBusy(false);
    }
  }
  return (
    <Modal title="New project" onClose={onClose} wide>
      <form onSubmit={submit} className="space-y-4">
        <div><label className="label" htmlFor="t">Title</label><input id="t" className="input" required value={title} onChange={(e) => setTitle(e.target.value)} /></div>
        <div><label className="label" htmlFor="d">Notes (optional)</label><textarea id="d" className="input" rows={2} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Where and when it was recorded, who performed…" /></div>
        <fieldset>
          <legend className="label">What should happen to the recording?</legend>
          <div className="grid gap-2">
            {MODES.map((m) => (
              <label key={m.key} className={`flex gap-3 rounded-lg border px-3 py-2.5 cursor-pointer transition ${mode === m.key ? "border-copper/70 bg-copper/5" : "border-ink-600 hover:border-ink-500"}`}>
                <input type="radio" name="mode" className="mt-1 accent-[#c98a5a]" checked={mode === m.key} onChange={() => setMode(m.key)} />
                <span><span className="text-sm font-medium">{m.title}</span>{m.tag && <span className="chip-live ml-2">{m.tag}</span>}<span className="block note mt-0.5">{m.text}</span></span>
              </label>
            ))}
          </div>
        </fieldset>
        <div className="grid sm:grid-cols-3 gap-3">
          <div><label className="label" htmlFor="l">Sung language</label>
            <select id="l" className="input" value={language} onChange={(e) => setLanguage(e.target.value)}><option value="la">Latin</option><option value="es">Spanish</option><option value="en">English</option></select></div>
          <div><label className="label" htmlFor="m">Rights: melody</label><input id="m" className="input" value={melody} onChange={(e) => setMelody(e.target.value)} /></div>
          <div><label className="label" htmlFor="r">Rights: recording</label><input id="r" className="input" value={recording} onChange={(e) => setRecording(e.target.value)} /></div>
        </div>
        <label className="flex gap-3 items-start callout-info cursor-pointer">
          <input type="checkbox" className="mt-1 accent-[#c98a5a]" checked={ack} onChange={(e) => setAck(e.target.checked)} />
          <span>I have the right to upload and process this recording. I understand it may contain identifiable voices, that a traditional melody can be public domain while a specific arrangement or recording has its own rights, and that it stays private to my account.</span>
        </label>
        <ErrorNote error={error} />
        <div className="flex justify-end gap-2"><button type="button" className="btn" onClick={onClose}>Cancel</button><button className="btn-primary" disabled={busy || !ack}>Create project</button></div>
      </form>
    </Modal>
  );
}

function ProjectCard({ p }: { p: Project }) {
  return (
    <Link to={`/p/${p.id}`} className="panel p-4 block hover:border-copper/50 transition group rise">
      <div className="flex items-start justify-between gap-3">
        <h3 className="h-display text-lg leading-snug group-hover:text-copper-soft transition">{p.title}</h3>
        {p.active_jobs > 0 ? <StatusChip status="running" /> : p.pending_gate === null ? <StatusChip status="final" /> : !p.source ? <StatusChip status="pending" label="Awaiting recording" /> : p.blockers > 0 ? <StatusChip status="blocked" label="Needs attention" /> : <StatusChip status="pending" label={`Gate ${p.pending_gate}`} />}
      </div>
      <div className="mt-1 text-[12px] text-ivory-mute">{MODE_LABEL[p.mode]} · updated {fmtDate(p.updated_at)}</div>
      <div className="mt-3 flex items-end gap-[2px] h-8" aria-hidden>
        {Array.from({ length: 48 }).map((_, i) => {
          const seed = (p.id.charCodeAt(i % p.id.length) + i * 7) % 23;
          return <span key={i} className="w-[3px] rounded-sm" style={{ height: `${18 + seed * 3.4}%`, background: p.source ? (i < 48 * ((7 - (p.pending_gate ?? 8) + 1) / 7) ? "#c98a5a" : "#343c52") : "#232938" }} />;
        })}
      </div>
      <div className="mt-3 text-[13px] text-ivory-dim"><span className="text-ivory-mute">Next: </span>{p.next_action}</div>
    </Link>
  );
}

export default function Dashboard() {
  const { data, error, reload } = useAsync<any>(() => api.get("/api/dashboard"), []);
  const [creating, setCreating] = useState(false);
  useEffect(() => {
    const t = setInterval(reload, 6000);
    return () => clearInterval(t);
  }, []);
  const projects: Project[] = data?.projects || [];
  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="h-display text-3xl">Project library</h1>
          <p className="note mt-1">Each project holds one recording, everything learned about it, and every version of its reconstruction.</p>
        </div>
        <button className="btn-primary" onClick={() => setCreating(true)}>New project</button>
      </div>
      <ErrorNote error={error} />
      {data && projects.length === 0 && (
        <Empty title="No projects yet">Create your first project, then upload the recording you want to reconstruct. The Adeste Fideles reference project is ready to create: it will wait for your MP3.
          <div className="mt-4"><button className="btn-primary" onClick={() => setCreating(true)}>Create the Adeste Fideles project</button></div>
        </Empty>
      )}
      {projects.length > 0 && <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-4">{projects.map((p) => <ProjectCard key={p.id} p={p} />)}</div>}
      {data && projects.length > 0 && (
        <div className="grid lg:grid-cols-3 gap-4">
          <Panel title="Waiting for your approval">
            {data.pending_approvals.length === 0 ? <p className="note">Nothing is waiting on you.</p> : (
              <ul className="space-y-2.5">{data.pending_approvals.map((a: any) => (
                <li key={a.project_id}><Link className="block hover:text-copper-soft transition" to={`/p/${a.project_id}`}><div className="text-sm">{a.title}</div><div className="text-[12px] text-ivory-mute">{a.next_action}</div></Link></li>
              ))}</ul>
            )}
          </Panel>
          <Panel title="Recent processing">
            {data.processing.length === 0 ? <p className="note">No agent has run yet.</p> : (
              <ul className="space-y-2">{data.processing.slice(0, 7).map((j: any) => (
                <li key={j.id} className="flex items-center justify-between gap-2 text-[13px]"><span className="truncate"><span className="text-ivory">{j.agent_name}</span><span className="text-ivory-mute"> · {j.project_title}</span></span><StatusChip status={j.status} /></li>
              ))}</ul>
            )}
          </Panel>
          <Panel title="Recent exports">
            {data.recent_exports.length === 0 ? <p className="note">Nothing exported yet.</p> : (
              <ul className="space-y-2">{data.recent_exports.map((e: any, i: number) => (
                <li key={i} className="text-[13px]"><div className="truncate">{e.message.replace("Exported ", "")}</div><div className="text-[12px] text-ivory-mute">{fmtDate(e.created_at)}</div></li>
              ))}</ul>
            )}
            {data.completed.length > 0 && <p className="note mt-3 pt-3 border-t border-ink-700/60">{data.completed.length} reconstruction(s) with an authorised final master.</p>}
          </Panel>
        </div>
      )}
      {creating && <NewProject onClose={() => setCreating(false)} />}
    </div>
  );
}
