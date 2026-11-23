import { useMemo, useState } from "react";
import { api } from "../../api";
import { useAsync, useDebounced } from "../../hooks";
import { useStore } from "../../store";
import { fmtInt, fmtOffset, fmtTime } from "../../format";
import { roleStyle } from "../../theme";
import type { SyslogRow } from "../../types";
import { MultiSelect } from "../ui/Select";
import { VirtualTable, type Column } from "../ui/VirtualTable";
import { Empty, ErrorState, Loading, TextInput, cx } from "../ui/primitives";
import { IconSearch } from "../ui/icons";

const LIMIT = 5000;

export function SyslogView({ device }: { device?: string }) {
  const { taskId, task, window: win, faultMs } = useStore();
  const [devices, setDevices] = useState<string[]>([]);
  const [programs, setPrograms] = useState<string[]>([]);
  const [q, setQ] = useState("");
  const dq = useDebounced(q, 250);
  const devFilter = device ? [device] : devices;
  const st = useAsync(
    taskId ? () => api.syslog(taskId, { device: devFilter, program: programs, q: dq || undefined, limit: LIMIT, start: win?.[0], end: win?.[1] }) : null,
    [taskId, devFilter.join(","), programs.join(","), dq, win?.[0], win?.[1]],
  );
  // Option lists come from an unfiltered query so the selectors stay stable.
  const opts = useAsync(taskId ? () => api.syslog(taskId, { limit: 1, device: device ? [device] : undefined }) : null, [taskId, device]);
  const roleOf = useMemo(() => new Map(task?.devices.map((d) => [d.name, d.role]) ?? []), [task]);
  const alertMs = task?.timeline.alert_time_ms ?? 0;
  const rows = useMemo(() => [...(st.data?.rows ?? [])].sort((a, b) => a.t - b.t), [st.data]);

  const cols: Column<SyslogRow>[] = [
    { key: "t", header: "Time (UTC)", width: "96px", render: (r) => <span className="font-mono text-fg-2 tabular">{fmtTime(r.t, true)}</span> },
    {
      key: "rel",
      header: "vs alert",
      width: "58px",
      align: "right",
      render: (r) => <span className={cx("font-mono tabular", r.t >= alertMs ? "text-alert" : "text-fg-3")}>{fmtOffset(r.t, alertMs)}</span>,
    },
    ...(!device
      ? [
          {
            key: "device",
            header: "Device",
            width: "150px",
            render: (r: SyslogRow) => (
              <span className="flex items-center gap-1.5 font-mono">
                <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: roleStyle(roleOf.get(r.device)).color }} />
                <span className="truncate text-fg-2">{r.device}</span>
              </span>
            ),
          },
        ]
      : []),
    { key: "program", header: "Program", width: "100px", render: (r) => <span className="font-mono text-accent">{r.program}</span> },
    { key: "msg", header: "Message", width: "minmax(0,1fr)", render: (r) => <span className="font-mono text-fg" title={r.message}>{r.message}</span> },
  ];

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
        {!device && (
          <MultiSelect className="w-52" options={(opts.data?.devices ?? []).map((d) => ({ value: d, color: roleStyle(roleOf.get(d)).color }))} value={devices} onChange={setDevices} placeholder="All devices" noun="devices" />
        )}
        <MultiSelect className="w-44" options={(opts.data?.programs ?? []).map((p) => ({ value: p }))} value={programs} onChange={setPrograms} placeholder="All programs" noun="programs" />
        <TextInput className="min-w-40 flex-1" value={q} onChange={setQ} placeholder="Search messages…" icon={<IconSearch size={12} />} />
        <span className="text-[10.5px] text-fg-3 tabular">
          {st.data ? (
            <>
              {fmtInt(st.data.matched)} matched{st.data.matched > rows.length ? ` · showing first ${fmtInt(rows.length)}` : ""} · {fmtInt(st.data.total)} total
              {win ? " · in window" : ""}
            </>
          ) : null}
        </span>
      </div>
      <div className="relative min-h-0 flex-1">
        {st.error ? (
          <ErrorState error={st.error} />
        ) : !st.data ? (
          <Loading label="Loading syslog" />
        ) : (
          <VirtualTable
            className="h-full"
            rows={rows}
            columns={cols}
            rowClassName={(r) => (faultMs && Math.abs(r.t - faultMs) < 1000 ? "bg-fault/10" : undefined)}
            empty={<Empty title="No syslog lines" hint={win || q || devices.length || programs.length ? "Nothing matches the filters or time window." : device ? `${device} sent no syslog.` : "No syslog was collected."} />}
          />
        )}
        {st.loading && st.data && (
          <div className="absolute right-3 top-9 rounded bg-surface-3 px-2 py-0.5 text-[10px] text-fg-3">updating…</div>
        )}
      </div>
    </div>
  );
}
