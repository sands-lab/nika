import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "./api";
import { useAsync, useLocalStorage } from "./hooks";
import type { Label, TaskDetail, TaskSummary } from "./types";
import type { ThemeName } from "./theme";

export type Selection = { kind: "device"; name: string } | { kind: "segment"; id: string } | null;
export type TimeWindow = [number, number] | null;
export type CollectorTab = "metrics" | "syslog" | "netflow" | "probes" | "int" | "controllers";
export type Layout = "split" | "topology" | "collectors";

interface Store {
  theme: ThemeName;
  setTheme: (t: ThemeName) => void;
  tasks: TaskSummary[] | undefined;
  tasksError: Error | undefined;
  taskId: string | null;
  setTaskId: (id: string) => void;
  task: TaskDetail | undefined;
  taskLoading: boolean;
  taskError: Error | undefined;
  revealed: boolean;
  setRevealed: (v: boolean) => void;
  label: Label | null | undefined; // undefined: not loaded / hidden, null: no label
  selection: Selection;
  select: (s: Selection) => void;
  window: TimeWindow;
  setWindow: (w: TimeWindow) => void;
  /** Effective x-range for charts: brushed window or full recording. */
  range: [number, number] | null;
  /** Fault injection time, only when revealed. */
  faultMs: number | null;
  collectorTab: CollectorTab;
  setCollectorTab: (t: CollectorTab) => void;
  layout: Layout;
  setLayout: (l: Layout) => void;
}

const Ctx = createContext<Store | null>(null);

function initialTaskId(): string | null {
  const h = decodeURIComponent(location.hash.replace(/^#\/?/, ""));
  return h || null;
}

export function StoreProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemeName>(() =>
    document.documentElement.classList.contains("dark") ? "dark" : "light",
  );
  const setTheme = useCallback((t: ThemeName) => {
    setThemeState(t);
    document.documentElement.classList.toggle("dark", t === "dark");
    try {
      localStorage.setItem("nika-explorer-theme", t);
    } catch {
      /* ignore */
    }
  }, []);

  const tasksState = useAsync(() => api.tasks(), []);
  const [taskId, setTaskIdState] = useState<string | null>(initialTaskId);
  const [revealedSet, setRevealedSet] = useState<Set<string>>(new Set());
  const [selection, setSelection] = useState<Selection>(null);
  const [window, setWindow] = useState<TimeWindow>(null);
  const [collectorTab, setCollectorTab] = useLocalStorage<CollectorTab>("nika-explorer-ctab", "metrics");
  const [layout, setLayout] = useLocalStorage<Layout>("nika-explorer-layout", "split");

  // Default to the first task once the list arrives.
  useEffect(() => {
    const list = tasksState.data;
    if (!list || list.length === 0) return;
    if (!taskId || !list.some((t) => t.task_id === taskId)) setTaskIdState(list[0].task_id);
  }, [tasksState.data, taskId]);

  const setTaskId = useCallback((id: string) => {
    setTaskIdState(id);
    setSelection(null);
    setWindow(null);
  }, []);

  useEffect(() => {
    if (taskId) history.replaceState(null, "", `#${encodeURIComponent(taskId)}`);
  }, [taskId]);

  const taskState = useAsync(taskId ? () => api.task(taskId) : null, [taskId]);
  const task = taskState.data && taskState.data.task_id === taskId ? taskState.data : undefined;
  const revealed = !!taskId && revealedSet.has(taskId);
  const setRevealed = useCallback(
    (v: boolean) => {
      if (!taskId) return;
      setRevealedSet((s) => {
        const n = new Set(s);
        if (v) n.add(taskId);
        else n.delete(taskId);
        return n;
      });
    },
    [taskId],
  );

  const labelState = useAsync<Label | null>(
    revealed && taskId
      ? () =>
          api.label(taskId).catch((e: { status?: number }) => {
            if (e.status === 404) return null;
            throw e;
          })
      : null,
    [revealed, taskId],
  );
  const label = revealed ? labelState.data : undefined;

  const range = useMemo<[number, number] | null>(() => {
    if (window) return window;
    if (!task) return null;
    return [task.timeline.recording_start_ms, task.timeline.recording_end_ms];
  }, [window, task]);

  const faultMs = revealed && label ? (label.fault_injected_at_ms ?? null) : null;

  const value: Store = {
    theme,
    setTheme,
    tasks: tasksState.data,
    tasksError: tasksState.error,
    taskId,
    setTaskId,
    task,
    taskLoading: taskState.loading,
    taskError: taskState.error,
    revealed,
    setRevealed,
    label,
    selection,
    select: setSelection,
    window,
    setWindow,
    range,
    faultMs,
    collectorTab,
    setCollectorTab,
    layout,
    setLayout,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useStore(): Store {
  const s = useContext(Ctx);
  if (!s) throw new Error("useStore outside StoreProvider");
  return s;
}

/** Parses a root-cause component string into device and optional port. */
export function parseComponent(c: string): { device: string; port?: string } | null {
  const parts = c.trim().split("/");
  if (parts[0] === "node" && parts[1]) return { device: parts[1] };
  if (parts[0] === "interface" && parts[1]) return { device: parts[1], port: parts.slice(2).join("/") || undefined };
  if (parts.length >= 2) return { device: parts[1], port: parts[2] };
  return null;
}
