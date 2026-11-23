import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { IconChevron, IconSearch, IconX } from "./icons";
import { cx } from "./primitives";

export interface Option {
  value: string;
  label?: string;
  hint?: ReactNode;
  color?: string;
}

function useOutside(ref: React.RefObject<HTMLElement | null>, onOut: () => void, active: boolean) {
  useEffect(() => {
    if (!active) return;
    const h = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onOut();
    };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [ref, onOut, active]);
}

const MAX_RENDER = 400;

function Dropdown({
  options,
  isSelected,
  onPick,
  query,
  setQuery,
  footer,
  onClose,
}: {
  options: Option[];
  isSelected: (v: string) => boolean;
  onPick: (v: string) => void;
  query: string;
  setQuery: (q: string) => void;
  footer?: ReactNode;
  onClose: () => void;
}) {
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return options;
    const terms = q.split(/\s+/);
    return options.filter((o) => {
      const s = `${o.value} ${o.label ?? ""}`.toLowerCase();
      return terms.every((t) => s.includes(t));
    });
  }, [options, query]);
  useEffect(() => setActive(0), [query]);
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [active]);
  return (
    <div className="absolute left-0 right-0 top-full z-50 mt-1 flex max-h-80 min-w-56 flex-col overflow-hidden rounded-md border border-line-strong bg-surface shadow-xl shadow-black/20">
      <div className="flex items-center gap-1.5 border-b border-line px-2 py-1.5">
        <IconSearch size={12} className="text-fg-3" />
        <input
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setActive((a) => Math.min(a + 1, Math.min(filtered.length, MAX_RENDER) - 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActive((a) => Math.max(a - 1, 0));
            } else if (e.key === "Enter") {
              e.preventDefault();
              if (filtered[active]) onPick(filtered[active].value);
            } else if (e.key === "Escape") onClose();
          }}
          placeholder="Search…"
          spellCheck={false}
          className="min-w-0 flex-1 bg-transparent text-xs text-fg outline-none placeholder:text-fg-3"
        />
        <span className="text-[10px] text-fg-3 tabular">{filtered.length}</span>
      </div>
      <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto py-1">
        {filtered.length === 0 && <div className="px-3 py-2 text-[11px] text-fg-3">No matches</div>}
        {filtered.slice(0, MAX_RENDER).map((o, i) => (
          <button
            key={o.value}
            data-idx={i}
            onMouseEnter={() => setActive(i)}
            onClick={() => onPick(o.value)}
            className={cx(
              "flex w-full items-center gap-2 px-2.5 py-1 text-left text-xs",
              i === active ? "bg-surface-3" : "",
              isSelected(o.value) ? "text-accent" : "text-fg-2",
            )}
          >
            <span className={cx("inline-flex h-3 w-3 shrink-0 items-center justify-center rounded-sm border text-[9px]", isSelected(o.value) ? "border-accent bg-accent text-white dark:text-black" : "border-line-strong")}>
              {isSelected(o.value) ? "✓" : ""}
            </span>
            {o.color && <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: o.color }} />}
            <span className="min-w-0 flex-1 truncate font-mono text-[11px]">{o.label ?? o.value}</span>
            {o.hint && <span className="shrink-0 text-[10px] text-fg-3">{o.hint}</span>}
          </button>
        ))}
        {filtered.length > MAX_RENDER && <div className="px-3 py-1 text-[10px] text-fg-3">{filtered.length - MAX_RENDER} more — refine the search</div>}
      </div>
      {footer && <div className="border-t border-line px-2 py-1.5">{footer}</div>}
    </div>
  );
}

export function SearchSelect({
  options,
  value,
  onChange,
  placeholder = "Select…",
  className,
}: {
  options: Option[];
  value: string | null;
  onChange: (v: string) => void;
  placeholder?: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  useOutside(ref, () => setOpen(false), open);
  const cur = options.find((o) => o.value === value);
  return (
    <div ref={ref} className={cx("relative", className)}>
      <button
        onClick={() => {
          setOpen((o) => !o);
          setQuery("");
        }}
        className="flex h-7 w-full items-center gap-2 rounded-md border border-line bg-surface-2 px-2 text-left text-xs hover:border-line-strong"
      >
        <span className={cx("min-w-0 flex-1 truncate font-mono text-[11px]", cur ? "text-fg" : "text-fg-3")}>{cur ? (cur.label ?? cur.value) : placeholder}</span>
        {cur?.hint && <span className="shrink-0 text-[10px] text-fg-3">{cur.hint}</span>}
        <IconChevron size={12} className="shrink-0 text-fg-3" />
      </button>
      {open && (
        <Dropdown
          options={options}
          isSelected={(v) => v === value}
          onPick={(v) => {
            onChange(v);
            setOpen(false);
          }}
          query={query}
          setQuery={setQuery}
          onClose={() => setOpen(false)}
        />
      )}
    </div>
  );
}

export function MultiSelect({
  options,
  value,
  onChange,
  placeholder = "All",
  className,
  noun = "items",
}: {
  options: Option[];
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
  className?: string;
  noun?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  useOutside(ref, () => setOpen(false), open);
  const sel = new Set(value);
  const summary =
    value.length === 0 ? placeholder : value.length <= 2 ? value.join(", ") : `${value.length} ${noun}`;
  return (
    <div ref={ref} className={cx("relative", className)}>
      <div className="flex h-7 w-full items-center gap-1 rounded-md border border-line bg-surface-2 pl-2 pr-1 text-xs hover:border-line-strong">
        <button onClick={() => setOpen((o) => !o)} className="flex min-w-0 flex-1 items-center gap-2 text-left">
          <span className={cx("min-w-0 flex-1 truncate font-mono text-[11px]", value.length ? "text-fg" : "text-fg-3")}>{summary}</span>
          <IconChevron size={12} className="shrink-0 text-fg-3" />
        </button>
        {value.length > 0 && (
          <button title="Clear" onClick={() => onChange([])} className="rounded p-0.5 text-fg-3 hover:bg-surface-3 hover:text-fg">
            <IconX size={11} />
          </button>
        )}
      </div>
      {open && (
        <Dropdown
          options={options}
          isSelected={(v) => sel.has(v)}
          onPick={(v) => {
            const n = new Set(sel);
            if (n.has(v)) n.delete(v);
            else n.add(v);
            onChange(options.map((o) => o.value).filter((x) => n.has(x)));
          }}
          query={query}
          setQuery={setQuery}
          onClose={() => setOpen(false)}
          footer={
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-fg-3">{value.length} selected</span>
              <div className="flex gap-2">
                <button className="text-fg-2 hover:text-accent" onClick={() => onChange(options.map((o) => o.value))}>
                  All
                </button>
                <button className="text-fg-2 hover:text-accent" onClick={() => onChange([])}>
                  None
                </button>
              </div>
            </div>
          }
        />
      )}
    </div>
  );
}
