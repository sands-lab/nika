import { useEffect, useState } from "react";
import { topologyPngUrl } from "../api";
import { useStore } from "../store";
import { TopologyGraph } from "./TopologyGraph";
import { Empty, Spinner, cx } from "./ui/primitives";

type Mode = "graph" | "diagram";

function Diagram({ taskId }: { taskId: string }) {
  const [actual, setActual] = useState(false);
  const [state, setState] = useState<"loading" | "ok" | "error">("loading");
  const url = topologyPngUrl(taskId);
  useEffect(() => {
    setState("loading");
    setActual(false);
  }, [taskId]);
  return (
    <div className={cx("absolute inset-0 z-10 bg-white", actual ? "overflow-auto" : "overflow-hidden")}>
      {state === "loading" && (
        <div className="absolute inset-0 flex items-center justify-center">
          <Spinner size={18} />
        </div>
      )}
      {state === "error" && <Empty title="Diagram not available" hint="The backend did not return a topology.png for this task." />}
      <img
        src={url}
        alt="Topology diagram"
        onLoad={() => setState("ok")}
        onError={() => setState("error")}
        onClick={() => setActual((a) => !a)}
        title={actual ? "Click to fit to panel" : "Click for actual size"}
        className={cx(state !== "ok" && "invisible", actual ? "max-w-none cursor-zoom-out" : "h-full w-full cursor-zoom-in object-contain p-2")}
      />
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        className="absolute bottom-2 right-2 rounded-md border border-slate-300 bg-white/90 px-2 py-1 text-[11px] text-slate-600 hover:text-slate-900"
      >
        Open full size ↗
      </a>
    </div>
  );
}

/** Topology area: interactive Cytoscape graph, or the pre-rendered diagram when the backend has one. */
export function TopologyPanel() {
  const { task } = useStore();
  const [mode, setMode] = useState<Mode>("graph");
  const hasPng = !!task?.has_topology_png;
  const shown: Mode = hasPng ? mode : "graph";
  return (
    <div className="relative h-full w-full">
      <TopologyGraph />
      {shown === "diagram" && task && <Diagram taskId={task.task_id} />}
      {hasPng && (
        <div className="absolute right-12 top-2 z-20 flex items-center rounded-md border border-line bg-surface/95 p-0.5 shadow-sm">
          {(["graph", "diagram"] as Mode[]).map((m) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              className={cx("h-6 rounded px-2.5 text-[11px] font-medium capitalize", shown === m ? "bg-surface-3 text-fg shadow-sm" : "text-fg-3 hover:text-fg-2")}
            >
              {m}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
