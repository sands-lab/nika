import { useStore, type Layout } from "../store";
import { fmtDateTime } from "../format";
import { Badge, cx } from "./ui/primitives";
import { IconChart, IconEye, IconEyeOff, IconGraph, IconLayout, IconMoon, IconSun } from "./ui/icons";

const LAYOUTS: { key: Layout; label: string; icon: typeof IconGraph }[] = [
  { key: "split", label: "Split", icon: IconLayout },
  { key: "topology", label: "Topology", icon: IconGraph },
  { key: "collectors", label: "Collectors", icon: IconChart },
];

export function Header() {
  const { theme, setTheme, task, revealed, setRevealed, label, layout, setLayout } = useStore();
  return (
    <header className="flex h-12 shrink-0 items-center gap-4 border-b border-line bg-surface px-3">
      <div className="flex shrink-0 items-center gap-2">
        <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-gradient-to-br from-teal-400 to-sky-500 text-white shadow-sm">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M6 19V5l12 14V5" />
          </svg>
        </div>
        <div className="leading-tight">
          <div className="text-[13px] font-semibold tracking-tight text-fg">NIKA</div>
          <div className="text-[10px] text-fg-3">dataset explorer</div>
        </div>
      </div>
      <div className="h-6 w-px bg-line" />
      <div className="flex min-w-0 flex-1 items-center gap-2.5">
        {task ? (
          <>
            <span className="truncate font-mono text-[12px] font-semibold text-fg">{task.task_id}</span>
            <Badge>{task.network}</Badge>
            {task.topology_size && <Badge>size {task.topology_size}</Badge>}
            <span className="hidden truncate text-[11px] text-fg-3 xl:inline">alert at {fmtDateTime(task.timeline.alert_time_ms)}</span>
          </>
        ) : (
          <span className="text-xs text-fg-3">No task selected</span>
        )}
      </div>
      {task && (
        <div className="flex shrink-0 items-center gap-2">
          {revealed && label === undefined && <span className="text-[11px] text-fg-3">loading answer…</span>}
          {revealed && label === null && <Badge>no label</Badge>}
          {revealed && label && (
            <div className="flex max-w-[520px] items-center gap-1.5 overflow-hidden">
              {label.is_anomaly ? <Badge tone="fault">anomaly</Badge> : <Badge tone="ok">healthy control</Badge>}
              {label.fault_category && <Badge tone="neutral">{label.fault_category}</Badge>}
              {label.root_causes.map((rc, i) => (
                <span key={i} className="flex min-w-0 items-center gap-1 truncate font-mono text-[11px]">
                  <span className="font-semibold text-fault">{rc.fault_type}</span>
                  <span className="text-fg-3">@</span>
                  <span className="truncate text-fg-2">{rc.component}</span>
                </span>
              ))}
            </div>
          )}
          <button
            onClick={() => setRevealed(!revealed)}
            className={cx(
              "flex h-7 items-center gap-1.5 rounded-md border px-2.5 text-xs font-medium transition-colors",
              revealed ? "border-fault/40 bg-fault/10 text-fault" : "border-line bg-surface-2 text-fg-2 hover:border-line-strong hover:text-fg",
            )}
            title="Show or hide the ground-truth label for this task"
          >
            {revealed ? <IconEyeOff size={13} /> : <IconEye size={13} />}
            {revealed ? "Hide answer" : "Reveal answer"}
          </button>
        </div>
      )}
      <div className="flex shrink-0 items-center rounded-md border border-line bg-surface-2 p-0.5">
        {LAYOUTS.map((l) => (
          <button
            key={l.key}
            title={`${l.label} view`}
            onClick={() => setLayout(l.key)}
            className={cx("flex h-6 items-center gap-1 rounded px-2 text-[11px] font-medium", layout === l.key ? "bg-surface-3 text-fg shadow-sm" : "text-fg-3 hover:text-fg-2")}
          >
            <l.icon size={12} />
            <span className="hidden lg:inline">{l.label}</span>
          </button>
        ))}
      </div>
      <button
        onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
        className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-line bg-surface-2 text-fg-2 hover:text-fg"
        title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
      >
        {theme === "dark" ? <IconSun size={14} /> : <IconMoon size={14} />}
      </button>
    </header>
  );
}
