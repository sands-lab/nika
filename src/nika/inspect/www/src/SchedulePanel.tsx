import type { ScheduleProgress } from "./api";
import {
  scheduleContext,
  schedulePhase,
  scheduleSeconds,
} from "./scheduleView";

export function SchedulePanel({
  schedule,
  compact = false,
  stage,
}: {
  schedule?: ScheduleProgress | null;
  compact?: boolean;
  stage?: string | null;
}) {
  if (!schedule) return null;
  const failures = schedule.occurrences.filter((o) => o.kind === "failure");
  const activeFailures = failures.filter(
    (o) => o.state === "active" || o.state === "stopping",
  ).length;
  const progress = schedule.total_operations
    ? schedule.completed_operations / schedule.total_operations
    : 1;
  const phase = schedulePhase(schedule, stage);
  const phaseState =
    stage && schedule.state === "complete" ? "running" : schedule.state;
  const context = scheduleContext(schedule, stage);
  if (compact)
    return (
      <section
        className="schedule-panel schedule-compact"
        aria-label="Schedule"
      >
        <div className="schedule-brief-grid">
          <div className="schedule-brief-cell schedule-phase-cell">
            <span className="schedule-caption">Scheduler</span>
            <span
              className={`schedule-state state-${phaseState}`}
              aria-label="Current phase"
            >
              {phase}
            </span>
          </div>
          <div
            className="schedule-brief-cell schedule-current-cell"
            title={`${context.currentLabel}: ${context.currentValue}`}
          >
            <span className="schedule-caption">{context.currentLabel}</span>
            <strong>{context.currentValue}</strong>
          </div>
          <div
            className="schedule-brief-cell schedule-next-cell"
            title={`${context.nextLabel}: ${context.nextValue}`}
          >
            <span className="schedule-caption">{context.nextLabel}</span>
            <strong>{context.nextValue}</strong>
          </div>
          <div className="schedule-brief-cell schedule-elapsed-cell">
            <span className="schedule-caption">Elapsed</span>
            <strong>{scheduleSeconds(schedule.elapsed_sec)}</strong>
          </div>
          <div className="schedule-brief-cell">
            <span className="schedule-caption">Operations</span>
            <strong>
              {schedule.completed_operations} / {schedule.total_operations}
            </strong>
          </div>
          <div className="schedule-brief-cell schedule-agent-cell">
            <span className="schedule-caption">Agent</span>
            <strong>
              {schedule.agent_actual_start != null
                ? `Started ${scheduleSeconds(schedule.agent_actual_start)}`
                : schedule.agent_start != null
                  ? `Starts ${scheduleSeconds(schedule.agent_start)}`
                  : "After initial checks"}
            </strong>
          </div>
        </div>
        {schedule.error && (
          <div className="schedule-error" role="alert">
            {schedule.error}
          </div>
        )}
      </section>
    );

  return (
    <section
      className="schedule-panel"
      aria-label="Schedule"
    >
      <div className="schedule-head">
        <div className="schedule-heading">
          <div className="schedule-eyebrow">
            Incident schedule{" "}
            <span className={`schedule-state state-${phaseState}`}>
              {phase}
            </span>
          </div>
          <div className="schedule-headline">
            {failures.length} failure windows{" "}
            <span>
              · {schedule.occurrences.length - failures.length} traffic windows
            </span>
          </div>
          <div
            className="schedule-progress"
            role="progressbar"
            aria-label="Scheduled operations"
            aria-valuenow={schedule.completed_operations}
            aria-valuemin={0}
            aria-valuemax={schedule.total_operations || 1}
          >
            <span style={{ width: `${progress * 100}%` }} />
          </div>
        </div>
        <div className="schedule-metrics">
          <div>
            <span>Operations</span>
            <strong>
              {schedule.completed_operations}
              <small> / {schedule.total_operations}</small>
            </strong>
          </div>
          <div
            className={
              activeFailures ? "schedule-kind-failure" : "schedule-clear"
            }
          >
            <span>Active failures</span>
            <strong>{activeFailures}</strong>
          </div>
          <div className="schedule-kind-agent">
            <span>Agent start</span>
            <strong>
              {scheduleSeconds(
                schedule.agent_actual_start ?? schedule.agent_start,
              )}
            </strong>
            <small>
              {schedule.agent_actual_start != null
                ? "observed"
                : schedule.agent_start == null
                  ? "after initial verification"
                  : "planned"}
            </small>
          </div>
        </div>
      </div>
      {schedule.next_label && (
        <div className="schedule-next">
          <span className="schedule-live-dot" />
          Next <strong>{scheduleSeconds(schedule.next_at)}</strong>
          <span title={schedule.next_label}>{schedule.next_label}</span>
        </div>
      )}
      {schedule.error && (
        <div className="schedule-error" role="alert">
          {schedule.error}
        </div>
      )}
      <div className="schedule-details">
        <header className="schedule-table-heading">
          Execution details <span>Planned and observed times</span>
        </header>
        <div className="schedule-table-scroll">
          <table className="schedule-table">
            <thead>
              <tr>
                <th>Event / target</th>
                <th>Occurrence</th>
                <th>State</th>
                <th>Planned start</th>
                <th>Applied</th>
                <th>Verified</th>
                <th>Planned stop</th>
                <th>Recovered / stopped</th>
              </tr>
            </thead>
            <tbody>
              {schedule.occurrences.map((event) => (
                <tr key={`${event.event_index}-${event.occurrence_index}`}>
                  <td>
                    <span
                      className={`schedule-event-label schedule-kind-${event.kind}`}
                    >
                      <i aria-hidden />
                      {event.label}
                    </span>
                  </td>
                  <td className="mono">
                    {event.occurrence_index + 1}
                    <span className="muted">/{event.occurrence_count}</span>
                  </td>
                  <td>
                    <span className={`schedule-state state-${event.state}`}>
                      {event.state}
                    </span>
                  </td>
                  <td>{scheduleSeconds(event.planned_start)}</td>
                  <td>{scheduleSeconds(event.actual_start)}</td>
                  <td>{scheduleSeconds(event.verified_at)}</td>
                  <td>
                    {event.planned_stop == null
                      ? "until teardown"
                      : scheduleSeconds(event.planned_stop)}
                  </td>
                  <td>{scheduleSeconds(event.actual_stop)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="schedule-note">
          Times are relative to scheduler start. Applied and verified times are
          controller observations. Ended means session teardown; recovery
          appears only when recorded.
        </p>
      </div>
    </section>
  );
}
