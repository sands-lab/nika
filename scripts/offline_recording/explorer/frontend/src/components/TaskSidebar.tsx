import { useMemo, useState } from "react";
import { useStore } from "../store";
import { fmtDuration } from "../format";
import { roleStyle } from "../theme";
import { Badge, Empty, ErrorState, Loading, TextInput, cx } from "./ui/primitives";
import { IconChevron, IconSearch } from "./ui/icons";

export function TaskSidebar() {
  const { tasks, tasksError, taskId, setTaskId, task } = useStore();
  const [q, setQ] = useState("");
  const [descOpen, setDescOpen] = useState(true);
  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase();
    return (tasks ?? []).filter((t) => !s || t.task_id.toLowerCase().includes(s) || t.network.toLowerCase().includes(s));
  }, [tasks, q]);

  const roleCounts = useMemo(() => {
    const m = new Map<string, number>();
    task?.devices.forEach((d) => m.set(d.role, (m.get(d.role) ?? 0) + 1));
    return [...m.entries()];
  }, [task]);

  return (
    <aside className="flex w-[264px] shrink-0 flex-col border-r border-line bg-surface">
      <div className="flex items-center justify-between px-3 pb-1.5 pt-2.5">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-fg-3">Tasks</span>
        {tasks && <span className="text-[10px] text-fg-3 tabular">{filtered.length}/{tasks.length}</span>}
      </div>
      <div className="px-2.5 pb-2">
        <TextInput value={q} onChange={setQ} placeholder="Filter tasks…" icon={<IconSearch size={12} />} />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-1.5 pb-2">
        {tasksError && <ErrorState error={tasksError} />}
        {!tasks && !tasksError && <Loading label="Loading tasks" />}
        {tasks && filtered.length === 0 && <Empty title="No tasks" hint={q ? "Nothing matches the filter." : "The dataset has no tasks."} />}
        {filtered.map((t) => {
          const active = t.task_id === taskId;
          const dur = Date.parse(t.recording_end) - Date.parse(t.recording_start);
          return (
            <button
              key={t.task_id}
              onClick={() => setTaskId(t.task_id)}
              className={cx(
                "group mb-0.5 flex w-full flex-col gap-1 rounded-md border px-2.5 py-2 text-left transition-colors",
                active ? "border-accent/40 bg-accent-soft" : "border-transparent hover:bg-surface-3",
              )}
            >
              <div className="flex w-full items-center gap-2">
                <span className={cx("min-w-0 flex-1 truncate text-[12px] font-semibold", active ? "text-fg" : "text-fg-2 group-hover:text-fg")}>{t.network}</span>
                {t.topology_size && <span className="text-[10px] text-fg-3">{t.topology_size}</span>}
              </div>
              <div className="truncate font-mono text-[10.5px] text-fg-3">{t.task_id}</div>
              <div className="flex flex-wrap items-center gap-1">
                <span className="text-[10px] text-fg-3 tabular">
                  {t.n_devices} dev · {t.n_segments} seg · {fmtDuration(dur)}
                </span>
              </div>
              <div className="flex flex-wrap gap-1">
                {t.telemetry.map((x) => (
                  <Badge key={x}>{x}</Badge>
                ))}
              </div>
            </button>
          );
        })}
      </div>
      {task && (
        <div className="max-h-[45%] shrink-0 overflow-hidden border-t border-line">
          <button onClick={() => setDescOpen((o) => !o)} className="flex w-full items-center justify-between px-3 py-2 text-[10px] font-semibold uppercase tracking-wider text-fg-3 hover:text-fg-2">
            Network description
            <IconChevron size={12} dir={descOpen ? "down" : "right"} />
          </button>
          {descOpen && (
            <div className="max-h-[calc(45vh-40px)] overflow-y-auto px-3 pb-3">
              <div className="mb-2 flex flex-wrap gap-x-2.5 gap-y-1">
                {roleCounts.map(([r, n]) => (
                  <span key={r} className="flex items-center gap-1 text-[10px] text-fg-2">
                    <span className="h-2 w-2 rounded-full" style={{ background: roleStyle(r).color }} />
                    {n} {r}
                  </span>
                ))}
              </div>
              <p className="whitespace-pre-wrap break-words text-[11px] leading-relaxed text-fg-2">{task.description.replace(/^Network Description:\s*/, "")}</p>
            </div>
          )}
        </div>
      )}
    </aside>
  );
}
