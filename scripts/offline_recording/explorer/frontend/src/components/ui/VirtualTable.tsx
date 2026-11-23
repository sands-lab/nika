import { useEffect, useRef, useState, type ReactNode } from "react";
import { cx } from "./primitives";

export interface Column<T> {
  key: string;
  header: ReactNode;
  width: string; // CSS grid track, e.g. "90px" or "minmax(0,1fr)"
  render: (row: T, i: number) => ReactNode;
  align?: "left" | "right";
  className?: string;
}

/** Fixed-row-height virtualized table built on CSS grid. */
export function VirtualTable<T>({
  rows,
  columns,
  rowHeight = 24,
  className,
  rowKey,
  onRowClick,
  rowClassName,
  empty,
}: {
  rows: T[];
  columns: Column<T>[];
  rowHeight?: number;
  className?: string;
  rowKey?: (row: T, i: number) => string | number;
  onRowClick?: (row: T) => void;
  rowClassName?: (row: T) => string | undefined;
  empty?: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [scroll, setScroll] = useState(0);
  const [height, setHeight] = useState(400);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setHeight(el.clientHeight));
    ro.observe(el);
    setHeight(el.clientHeight);
    return () => ro.disconnect();
  }, []);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = 0;
    setScroll(0);
  }, [rows]);
  const template = columns.map((c) => c.width).join(" ");
  const start = Math.max(0, Math.floor(scroll / rowHeight) - 10);
  const end = Math.min(rows.length, Math.ceil((scroll + height) / rowHeight) + 10);
  return (
    <div className={cx("flex min-h-0 flex-col", className)}>
      <div className="grid shrink-0 border-b border-line bg-surface-2 text-[10px] font-semibold uppercase tracking-wide text-fg-3" style={{ gridTemplateColumns: template }}>
        {columns.map((c) => (
          <div key={c.key} className={cx("truncate px-2 py-1.5", c.align === "right" && "text-right")}>
            {c.header}
          </div>
        ))}
      </div>
      <div ref={ref} className="relative min-h-0 flex-1 overflow-auto" onScroll={(e) => setScroll(e.currentTarget.scrollTop)}>
        {rows.length === 0 ? (
          empty
        ) : (
          <div style={{ height: rows.length * rowHeight, position: "relative" }}>
            {rows.slice(start, end).map((r, j) => {
              const i = start + j;
              return (
                <div
                  key={rowKey ? rowKey(r, i) : i}
                  onClick={onRowClick ? () => onRowClick(r) : undefined}
                  className={cx(
                    "absolute left-0 right-0 grid items-center border-b border-line/60 text-[11px] hover:bg-surface-3/70",
                    onRowClick && "cursor-pointer",
                    rowClassName?.(r),
                  )}
                  style={{ top: i * rowHeight, height: rowHeight, gridTemplateColumns: template }}
                >
                  {columns.map((c) => (
                    <div key={c.key} className={cx("min-w-0 truncate px-2", c.align === "right" && "text-right tabular", c.className)}>
                      {c.render(r, i)}
                    </div>
                  ))}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
