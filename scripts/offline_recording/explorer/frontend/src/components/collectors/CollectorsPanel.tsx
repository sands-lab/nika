import { useEffect } from "react";
import { useStore, type CollectorTab } from "../../store";
import { Tabs, type TabDef } from "../ui/primitives";
import { MetricsBrowser } from "./MetricsBrowser";
import { SyslogView } from "./SyslogView";
import { NetflowView } from "./NetflowView";
import { ProbesView } from "./ProbesView";
import { IntView } from "./IntView";
import { ControllersView } from "./ControllersView";

export function CollectorsPanel({ right }: { right?: React.ReactNode }) {
  const { task, collectorTab, setCollectorTab } = useStore();
  const c = task?.collectors;
  const hasProbes = !!c && (c.pingmesh || c.http_dns || c.iperf || c.traceroute);
  const tabs: TabDef<CollectorTab>[] = [
    { key: "metrics", label: "Metrics", hidden: !c?.prometheus },
    { key: "syslog", label: "Syslog", hidden: !c?.syslog },
    { key: "netflow", label: "NetFlow", hidden: !c?.netflow },
    { key: "probes", label: "Probes", hidden: !hasProbes },
    { key: "int", label: "INT", hidden: !c?.int },
    { key: "controllers", label: `Controllers${task?.controllers.length ? ` (${task.controllers.length})` : ""}`, hidden: !task?.controllers.length },
  ];
  const visible = tabs.filter((t) => !t.hidden);
  const active = visible.some((t) => t.key === collectorTab) ? collectorTab : visible[0]?.key;
  useEffect(() => {
    if (active && active !== collectorTab) setCollectorTab(active);
  }, [active, collectorTab, setCollectorTab]);

  return (
    <div className="flex h-full min-h-0 flex-col bg-surface">
      <Tabs tabs={tabs} value={active ?? "metrics"} onChange={setCollectorTab} className="px-2" right={right} />
      <div className="min-h-0 flex-1">
        {!task ? null : visible.length === 0 ? (
          <div className="p-4 text-xs text-fg-3">No collectors recorded for this task.</div>
        ) : (
          <>
            {active === "metrics" && <MetricsBrowser key={task.task_id} />}
            {active === "syslog" && <SyslogView key={task.task_id} />}
            {active === "netflow" && <NetflowView key={task.task_id} />}
            {active === "probes" && <ProbesView key={task.task_id} />}
            {active === "int" && <IntView key={task.task_id} />}
            {active === "controllers" && <ControllersView key={task.task_id} />}
          </>
        )}
      </div>
    </div>
  );
}
