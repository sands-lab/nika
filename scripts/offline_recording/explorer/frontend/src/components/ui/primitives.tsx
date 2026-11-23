import type { ButtonHTMLAttributes, ReactNode } from "react";
import { IconAlert } from "./icons";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

export function Spinner({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" className="animate-spin text-fg-3" aria-label="Loading">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" fill="none" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" fill="none" strokeLinecap="round" />
    </svg>
  );
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="flex h-full min-h-16 w-full items-center justify-center gap-2 text-xs text-fg-3">
      <Spinner /> {label}…
    </div>
  );
}

export function Empty({ title, hint, icon }: { title: string; hint?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex h-full min-h-16 w-full flex-col items-center justify-center gap-1 p-4 text-center">
      {icon && <div className="mb-1 text-fg-3">{icon}</div>}
      <div className="text-xs font-medium text-fg-2">{title}</div>
      {hint && <div className="max-w-sm text-[11px] text-fg-3">{hint}</div>}
    </div>
  );
}

export function ErrorState({ error }: { error: Error }) {
  return (
    <div className="flex h-full min-h-16 w-full items-center justify-center p-4">
      <div className="flex max-w-md items-start gap-2 rounded-md border border-fault/30 bg-fault/10 px-3 py-2 text-xs text-fault">
        <IconAlert size={14} className="mt-px shrink-0" />
        <span className="break-words">{error.message}</span>
      </div>
    </div>
  );
}

export function Badge({ children, tone = "neutral", title }: { children: ReactNode; tone?: "neutral" | "accent" | "alert" | "fault" | "ok"; title?: string }) {
  const tones = {
    neutral: "border-line-strong/70 bg-surface-3 text-fg-2",
    accent: "border-accent/30 bg-accent-soft text-accent",
    alert: "border-alert/30 bg-alert/10 text-alert",
    fault: "border-fault/30 bg-fault/10 text-fault",
    ok: "border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  };
  return (
    <span title={title} className={cx("inline-flex items-center gap-1 rounded border px-1.5 py-px text-[10px] font-medium leading-4 whitespace-nowrap", tones[tone])}>
      {children}
    </span>
  );
}

export function Button({ className, active, size = "sm", ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { active?: boolean; size?: "xs" | "sm" }) {
  return (
    <button
      {...rest}
      className={cx(
        "inline-flex items-center justify-center gap-1.5 rounded-md border font-medium transition-colors select-none disabled:cursor-not-allowed disabled:opacity-50",
        size === "xs" ? "h-6 px-2 text-[11px]" : "h-7 px-2.5 text-xs",
        active
          ? "border-accent/40 bg-accent-soft text-accent"
          : "border-line bg-surface-2 text-fg-2 hover:border-line-strong hover:bg-surface-3 hover:text-fg",
        className,
      )}
    />
  );
}

export interface TabDef<K extends string> {
  key: K;
  label: ReactNode;
  disabled?: boolean;
  hidden?: boolean;
  title?: string;
}

export function Tabs<K extends string>({
  tabs,
  value,
  onChange,
  size = "md",
  className,
  right,
}: {
  tabs: TabDef<K>[];
  value: K;
  onChange: (k: K) => void;
  size?: "sm" | "md";
  className?: string;
  right?: ReactNode;
}) {
  return (
    <div className={cx("flex min-w-0 items-end gap-0.5 border-b border-line", className)}>
      <div className="flex min-w-0 flex-1 items-end gap-0.5 overflow-x-auto">
        {tabs
          .filter((t) => !t.hidden)
          .map((t) => (
            <button
              key={t.key}
              title={t.title}
              disabled={t.disabled}
              onClick={() => onChange(t.key)}
              className={cx(
                "relative shrink-0 whitespace-nowrap border-b-2 font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40",
                size === "sm" ? "px-2 pb-1.5 pt-1 text-[11px]" : "px-3 pb-2 pt-1.5 text-xs",
                value === t.key ? "border-accent text-fg" : "border-transparent text-fg-3 hover:text-fg-2",
              )}
            >
              {t.label}
            </button>
          ))}
      </div>
      {right && <div className="flex shrink-0 items-center gap-1.5 pb-1">{right}</div>}
    </div>
  );
}

export function Panel({ title, right, children, className, bodyClassName }: { title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string; bodyClassName?: string }) {
  return (
    <div className={cx("flex min-h-0 min-w-0 flex-col rounded-lg border border-line bg-surface", className)}>
      {(title || right) && (
        <div className="flex min-h-8 items-center justify-between gap-2 border-b border-line px-3 py-1.5">
          <div className="min-w-0 truncate text-[11px] font-semibold uppercase tracking-wide text-fg-3">{title}</div>
          {right && <div className="flex shrink-0 items-center gap-1.5">{right}</div>}
        </div>
      )}
      <div className={cx("min-h-0 flex-1", bodyClassName)}>{children}</div>
    </div>
  );
}

export function TextInput({
  value,
  onChange,
  placeholder,
  className,
  icon,
  right,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  className?: string;
  icon?: ReactNode;
  right?: ReactNode;
}) {
  return (
    <div className={cx("flex h-7 items-center gap-1.5 rounded-md border border-line bg-surface-2 px-2 focus-within:border-accent/60", className)}>
      {icon && <span className="shrink-0 text-fg-3">{icon}</span>}
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        spellCheck={false}
        className="h-full min-w-0 flex-1 bg-transparent text-xs text-fg outline-none placeholder:text-fg-3"
      />
      {right}
    </div>
  );
}

export function Toggle({ checked, onChange, label, title }: { checked: boolean; onChange: (v: boolean) => void; label: ReactNode; title?: string }) {
  return (
    <label title={title} className="inline-flex cursor-pointer select-none items-center gap-1.5 text-[11px] text-fg-2">
      <span
        className={cx("relative inline-block h-3.5 w-6 rounded-full border transition-colors", checked ? "border-accent/50 bg-accent/70" : "border-line-strong bg-surface-3")}
      >
        <span className={cx("absolute top-px h-2.5 w-2.5 rounded-full bg-white shadow transition-all", checked ? "left-[11px]" : "left-px")} />
      </span>
      <input type="checkbox" className="sr-only" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}

export function RoleDot({ color, size = 8 }: { color: string; size?: number }) {
  return <span className="inline-block shrink-0 rounded-full" style={{ width: size, height: size, background: color }} />;
}
