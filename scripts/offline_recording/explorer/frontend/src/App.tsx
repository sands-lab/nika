import { useRef } from "react";
import { StoreProvider, useStore } from "./store";
import { useLocalStorage } from "./hooks";
import { Header } from "./components/Header";
import { TaskSidebar } from "./components/TaskSidebar";
import { TimelineBar } from "./components/TimelineBar";
import { TopologyPanel } from "./components/TopologyPanel";
import { CollectorsPanel } from "./components/collectors/CollectorsPanel";
import { SidePanel } from "./components/panel/SidePanel";
import { Empty, ErrorState, Loading } from "./components/ui/primitives";
import { IconChevron } from "./components/ui/icons";

function Main() {
  const { task, taskError, taskLoading, tasks, layout, setLayout } = useStore();
  const [split, setSplit] = useLocalStorage<number>("nika-explorer-split", 0.5);
  const box = useRef<HTMLDivElement>(null);

  const startDrag = (e: React.PointerEvent) => {
    e.preventDefault();
    const el = box.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const move = (ev: PointerEvent) => {
      const f = (ev.clientY - rect.top) / rect.height;
      setSplit(Math.min(0.8, Math.max(0.2, f)));
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      document.body.style.cursor = "";
    };
    document.body.style.cursor = "row-resize";
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  if (taskError) return <ErrorState error={taskError} />;
  if (!task) return taskLoading || !tasks ? <Loading label="Loading task" /> : <Empty title="Pick a task" />;

  const showTopo = layout !== "collectors";
  const showColl = layout !== "topology";
  return (
    <div ref={box} className="flex h-full min-h-0 min-w-0 flex-col">
      {showTopo && (
        <div className="relative min-h-0 bg-bg" style={{ flex: showColl ? `0 0 ${split * 100}%` : "1 1 auto" }}>
          <TopologyPanel />
        </div>
      )}
      {showTopo && showColl && (
        <div onPointerDown={startDrag} className="group relative z-10 h-1.5 shrink-0 cursor-row-resize border-y border-line bg-surface-2 hover:bg-accent/30" title="Drag to resize">
          <div className="absolute left-1/2 top-1/2 h-0.5 w-8 -translate-x-1/2 -translate-y-1/2 rounded bg-line-strong group-hover:bg-accent" />
        </div>
      )}
      {showColl && (
        <div className="min-h-0 flex-1">
          <CollectorsPanel
            right={
              <button
                className="flex h-6 items-center gap-1 rounded px-1.5 text-[11px] text-fg-3 hover:bg-surface-3 hover:text-fg"
                onClick={() => setLayout(layout === "collectors" ? "split" : "collectors")}
                title={layout === "collectors" ? "Show topology" : "Maximize collectors"}
              >
                <IconChevron size={12} dir={layout === "collectors" ? "down" : "up"} />
                {layout === "collectors" ? "Restore" : "Maximize"}
              </button>
            }
          />
        </div>
      )}
    </div>
  );
}

function Shell() {
  return (
    <div className="flex h-full flex-col overflow-hidden">
      <Header />
      <TimelineBar />
      <div className="flex min-h-0 flex-1">
        <TaskSidebar />
        <main className="min-h-0 min-w-0 flex-1">
          <Main />
        </main>
        <SidePanel />
      </div>
    </div>
  );
}

export default function App() {
  return (
    <StoreProvider>
      <Shell />
    </StoreProvider>
  );
}
