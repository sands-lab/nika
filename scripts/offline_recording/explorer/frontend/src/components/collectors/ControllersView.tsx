import { useEffect, useState } from "react";
import { api } from "../../api";
import { useAsync } from "../../hooks";
import { useStore } from "../../store";
import { Empty, ErrorState, Loading, cx } from "../ui/primitives";

export function ControllersView() {
  const { taskId, task } = useStore();
  const files = task?.controllers ?? [];
  const [name, setName] = useState<string | null>(files[0] ?? null);
  useEffect(() => {
    if (!name || !files.includes(name)) setName(files[0] ?? null);
  }, [files, name]);
  const st = useAsync(taskId && name ? () => api.controller(taskId, name) : null, [taskId, name]);
  if (!files.length) return <Empty title="No controller state captured" />;
  return (
    <div className="flex h-full min-h-0">
      <div className="w-56 shrink-0 overflow-y-auto border-r border-line p-1.5">
        {files.map((f) => (
          <button
            key={f}
            onClick={() => setName(f)}
            className={cx("block w-full truncate rounded px-2 py-1 text-left font-mono text-[11px]", f === name ? "bg-accent-soft text-accent" : "text-fg-2 hover:bg-surface-3")}
          >
            {f}
          </button>
        ))}
      </div>
      <div className="min-h-0 min-w-0 flex-1 overflow-auto">
        {st.error ? (
          <ErrorState error={st.error} />
        ) : !st.data ? (
          <Loading />
        ) : (
          <pre className="px-3 py-2 font-mono text-[11px] leading-relaxed text-fg-2">{st.data.text}</pre>
        )}
      </div>
    </div>
  );
}
