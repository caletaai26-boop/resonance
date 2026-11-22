import React, { createContext, useContext, useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Link, NavLink, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import "./index.css";
import { api } from "./api";
import { Logo } from "./components/ui";
import Dashboard from "./pages/Dashboard";
import Login from "./pages/Login";
import Settings from "./pages/Settings";
import Studio from "./pages/Studio";

type User = { id: string; email: string; name: string };
const AuthCtx = createContext<{ user: User | null; setUser: (u: User | null) => void }>({ user: null, setUser: () => {} });
export const useAuth = () => useContext(AuthCtx);

export function Shell({ children, wide }: { children: React.ReactNode; wide?: boolean }) {
  const { user, setUser } = useAuth();
  const nav = useNavigate();
  return (
    <div className="min-h-full flex flex-col">
      <header className="sticky top-0 z-40 border-b border-ink-700/70 bg-ink-950/85 backdrop-blur">
        <div className={`mx-auto flex items-center gap-6 px-5 h-14 ${wide ? "" : "max-w-6xl"}`}>
          <Link to="/" aria-label="RESONANCE home"><Logo /></Link>
          <nav className="flex items-center gap-1 text-sm">
            <NavLink to="/" end className={({ isActive }) => `px-3 py-1.5 rounded-md transition ${isActive ? "text-ivory bg-ink-800" : "text-ivory-mute hover:text-ivory"}`}>Projects</NavLink>
            <NavLink to="/settings" className={({ isActive }) => `px-3 py-1.5 rounded-md transition ${isActive ? "text-ivory bg-ink-800" : "text-ivory-mute hover:text-ivory"}`}>Engines &amp; settings</NavLink>
          </nav>
          <div className="ml-auto flex items-center gap-3 text-sm text-ivory-mute">
            <span className="hidden sm:inline">{user?.name}</span>
            <button className="btn-ghost btn-sm" onClick={async () => { await api.post("/api/auth/logout"); setUser(null); nav("/login"); }}>Sign out</button>
          </div>
        </div>
      </header>
      <main className={`flex-1 w-full mx-auto ${wide ? "" : "max-w-6xl px-5 py-8"}`}>{children}</main>
    </div>
  );
}

function App() {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    fetch("/api/auth/me", { credentials: "same-origin" }).then((r) => (r.ok ? r.json() : null)).then(setUser).finally(() => setReady(true));
  }, []);
  if (!ready) return <div className="h-full grid place-items-center text-ivory-mute text-sm">Loading…</div>;
  const guard = (el: React.ReactElement) => (user ? el : <Navigate to="/login" replace />);
  return (
    <AuthCtx.Provider value={{ user, setUser }}>
      <Routes>
        <Route path="/login" element={user ? <Navigate to="/" replace /> : <Login />} />
        <Route path="/" element={guard(<Shell><Dashboard /></Shell>)} />
        <Route path="/settings" element={guard(<Shell><Settings /></Shell>)} />
        <Route path="/p/:id/*" element={guard(<Shell wide><Studio /></Shell>)} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AuthCtx.Provider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
