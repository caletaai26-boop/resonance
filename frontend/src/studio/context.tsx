import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { AgentView, Job, Project, Status, api } from "../api";

export type Player = {
  time: number; duration: number; playing: boolean; rate: number; loop: [number, number] | null;
  toggle: () => void; play: () => void; pause: () => void; seek: (t: number) => void; setRate: (r: number) => void; setLoop: (l: [number, number] | null) => void;
  ready: boolean;
};

/** One HTMLAudioElement with a requestAnimationFrame clock, loop region and speed control. */
export function usePlayer(src: string | null): Player {
  const ref = useRef<HTMLAudioElement | null>(null);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [rate, setRateState] = useState(1);
  const [ready, setReady] = useState(false);
  const loopRef = useRef<[number, number] | null>(null);
  const [loop, setLoopState] = useState<[number, number] | null>(null);

  useEffect(() => {
    if (!src) return;
    const a = new Audio(src);
    a.preload = "metadata";
    ref.current = a;
    setReady(false); setTime(0); setPlaying(false);
    const onMeta = () => { setDuration(a.duration || 0); setReady(true); };
    const onEnd = () => setPlaying(false);
    a.addEventListener("loadedmetadata", onMeta);
    a.addEventListener("ended", onEnd);
    a.addEventListener("pause", onEnd);
    a.addEventListener("play", () => setPlaying(true));
    let raf = 0;
    const tick = () => {
      const l = loopRef.current;
      if (l && !a.paused && a.currentTime >= l[1]) a.currentTime = l[0];
      setTime(a.currentTime);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => { cancelAnimationFrame(raf); a.pause(); a.src = ""; ref.current = null; };
  }, [src]);

  const play = useCallback(() => { const a = ref.current; if (!a) return; const l = loopRef.current; if (l && (a.currentTime < l[0] || a.currentTime >= l[1])) a.currentTime = l[0]; a.play().catch(() => {}); }, []);
  const pause = useCallback(() => ref.current?.pause(), []);
  const toggle = useCallback(() => (ref.current?.paused ? play() : pause()), [play, pause]);
  const seek = useCallback((t: number) => { const a = ref.current; if (a) { a.currentTime = Math.max(0, Math.min(t, a.duration || t)); setTime(a.currentTime); } }, []);
  const setRate = useCallback((r: number) => { if (ref.current) { ref.current.playbackRate = r; (ref.current as any).preservesPitch = true; } setRateState(r); }, []);
  const setLoop = useCallback((l: [number, number] | null) => { loopRef.current = l; setLoopState(l); }, []);
  return { time, duration, playing, rate, loop, toggle, play, pause, seek, setRate, setLoop, ready };
}

export type Studio = {
  project: Project; status: Status; agents: AgentView[]; caps: any; jobs: Job[];
  refresh: () => Promise<void>; version: number;
  runAgent: (agent: string, params?: any) => Promise<void>; runPipeline: (name: string, params?: any) => Promise<void>;
  error: string | null; setError: (e: string | null) => void; flash: (m: string) => void; notice: string | null;
  player: Player; working: boolean; agent: (key: string) => AgentView | undefined;
  selectedAgent: string | null; selectAgent: (k: string | null) => void;
};

export const StudioCtx = createContext<Studio | null>(null);
export const useStudio = () => useContext(StudioCtx)!;

/** Project state + live job stream. `version` increments whenever jobs change, so tabs can refetch their documents. */
export function useStudioState(id: string) {
  const [project, setProject] = useState<Project | null>(null);
  const [agents, setAgents] = useState<AgentView[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [caps, setCaps] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [missing, setMissing] = useState(false);
  const sig = useRef("");

  const refresh = useCallback(async () => {
    try {
      const [p, a] = await Promise.all([api.get(`/api/projects/${id}`), api.get(`/api/projects/${id}/agents`)]);
      setProject(p); setAgents(a); setVersion((v) => v + 1);
    } catch (e: any) {
      if (e.status === 404) setMissing(true); else setError(e.message);
    }
  }, [id]);

  useEffect(() => { refresh(); api.get("/api/capabilities").then(setCaps).catch(() => {}); }, [refresh]);

  useEffect(() => {
    const es = new EventSource(`/api/projects/${id}/events`);
    es.addEventListener("jobs", (ev: MessageEvent) => {
      const list: Job[] = JSON.parse(ev.data);
      setJobs(list);
      // live progress goes straight into the agent cards; a status change triggers a full refetch
      setAgents((prev) => prev.map((a) => { const j = list.find((x) => x.agent === a.key); return j && a.job && j.id === a.job.id ? { ...a, status: j.status, job: { ...a.job, ...j } } : a; }));
      const s = list.map((j) => `${j.id}:${j.status}`).join("|");
      if (s !== sig.current) { sig.current = s; refresh(); }
    });
    return () => es.close();
  }, [id, refresh]);

  const flash = useCallback((m: string) => { setNotice(m); setTimeout(() => setNotice((n) => (n === m ? null : n)), 6000); }, []);
  const runAgent = useCallback(async (agent: string, params: any = {}) => {
    try { setError(null); await api.post(`/api/projects/${id}/agents/${agent}/run`, { params }); await refresh(); } catch (e: any) { setError(e.message); }
  }, [id, refresh]);
  const runPipeline = useCallback(async (name: string, params: any = {}) => {
    try { setError(null); await api.post(`/api/projects/${id}/pipeline/${name}`, { params }); await refresh(); } catch (e: any) { setError(e.message); }
  }, [id, refresh]);

  return { project, agents, jobs, caps, error, setError, notice, flash, version, refresh, runAgent, runPipeline, missing };
}
