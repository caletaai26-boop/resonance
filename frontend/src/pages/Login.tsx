import { FormEvent, useState } from "react";
import { api } from "../api";
import { ErrorNote, Logo } from "../components/ui";
import { useAuth } from "../main";

export default function Login() {
  const { setUser } = useAuth();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setUser(await api.post(`/api/auth/${mode}`, { email, password, name }));
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-full grid lg:grid-cols-[1.1fr_1fr]">
      <div className="hidden lg:flex flex-col justify-between p-12 border-r border-ink-700/60 relative overflow-hidden">
        <Logo size={26} />
        <svg className="absolute inset-x-0 top-1/3 w-full opacity-70" viewBox="0 0 800 200" preserveAspectRatio="none" aria-hidden>
          {Array.from({ length: 160 }).map((_, i) => {
            const h = 14 + Math.abs(Math.sin(i * 0.21) * 60 + Math.sin(i * 0.057) * 70) * (0.4 + 0.6 * Math.abs(Math.sin(i * 0.013 + 1)));
            return <rect key={i} x={i * 5} y={100 - h / 2} width="2.2" height={h} rx="1.1" fill={i < 62 ? "#c98a5a" : "#3a4259"} />;
          })}
        </svg>
        <div className="relative max-w-md">
          <h1 className="h-display text-4xl leading-tight">The music is the truth.<br />The recording is only how you heard it.</h1>
          <p className="note mt-4">Upload an imperfect recording of a live performance. RESONANCE analyses it, helps you verify exactly what was played and sung, and rebuilds that same arrangement with clean, new sound. Nothing is invented without your approval.</p>
        </div>
      </div>
      <div className="grid place-items-center p-6">
        <form onSubmit={submit} className="panel w-full max-w-sm p-7 space-y-4 rise">
          <div className="lg:hidden mb-2"><Logo /></div>
          <h2 className="h-display text-2xl">{mode === "login" ? "Sign in" : "Create your studio account"}</h2>
          {mode === "register" && (
            <div><label className="label" htmlFor="name">Name</label><input id="name" className="input" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" /></div>
          )}
          <div><label className="label" htmlFor="email">Email</label><input id="email" type="email" required className="input" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" /></div>
          <div>
            <label className="label" htmlFor="pw">Password {mode === "register" && <span className="text-ivory-mute">(at least 8 characters)</span>}</label>
            <input id="pw" type="password" required minLength={8} className="input" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete={mode === "login" ? "current-password" : "new-password"} />
          </div>
          <ErrorNote error={error} />
          <button className="btn-primary w-full" disabled={busy}>{busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}</button>
          <p className="text-[13px] text-ivory-mute text-center">
            {mode === "login" ? "First time here? " : "Already have an account? "}
            <button type="button" className="text-copper-soft hover:underline" onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(null); }}>
              {mode === "login" ? "Create an account" : "Sign in"}
            </button>
          </p>
          <p className="text-[11px] text-ivory-mute leading-relaxed border-t border-ink-700/70 pt-3">Projects are private to your account. Recordings stay on this server and are never used to train models.</p>
        </form>
      </div>
    </div>
  );
}
