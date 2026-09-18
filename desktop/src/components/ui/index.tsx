// Shared primitives. Small on purpose: a button, a field row, a panel, a
// dialog, a badge. Anything more specific lives with the view that needs it.

import {
  useEffect,
  useId,
  useRef,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
} from "react";
import { Icons } from "../icons";
import styles from "./ui.module.css";

// --- Button ----------------------------------------------------------------

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "default" | "primary" | "danger" | "ghost";
  size?: "sm" | "md" | "lg";
};

export function Button({ variant = "default", size = "md", className, ...rest }: ButtonProps) {
  return (
    <button
      type="button"
      className={[styles.button, className].filter(Boolean).join(" ")}
      data-variant={variant}
      data-size={size}
      {...rest}
    />
  );
}

export function IconButton({ className, ...rest }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type="button"
      className={[styles.iconButton, className].filter(Boolean).join(" ")}
      {...rest}
    />
  );
}

// --- Fields ----------------------------------------------------------------

interface FieldProps {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  stacked?: boolean;
  children: ReactNode;
}

/** Label on the left, control on the right — the dense form layout an
 *  instrument panel wants. `stacked` puts the label above for wide controls. */
export function Field({ label, hint, error, stacked = false, children }: FieldProps) {
  return (
    <label className={styles.field} data-stacked={stacked}>
      <span className={styles.fieldLabel}>{label}</span>
      {children}
      {hint ? <span className={styles.fieldHint}>{hint}</span> : null}
      {error ? <span className={styles.fieldError}>{error}</span> : null}
    </label>
  );
}

type InputProps = InputHTMLAttributes<HTMLInputElement> & { invalid?: boolean; unit?: string };

export function Input({ invalid, unit, className, ...rest }: InputProps) {
  const control = (
    <input
      className={[styles.control, className].filter(Boolean).join(" ")}
      data-invalid={invalid || undefined}
      spellCheck={false}
      {...rest}
    />
  );
  if (!unit) return control;
  return (
    <span className={styles.unitControl}>
      {control}
      <span className={styles.unit}>{unit}</span>
    </span>
  );
}

export function Select({ className, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={[styles.control, className].filter(Boolean).join(" ")} {...rest} />;
}

interface ToggleProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  label?: string;
}

export function Toggle({ checked, onChange, disabled, label }: ToggleProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className={styles.toggle}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    />
  );
}

// --- Panel -----------------------------------------------------------------

interface PanelProps {
  title?: ReactNode;
  actions?: ReactNode;
  className?: string;
  bodyClassName?: string;
  children: ReactNode;
}

export function Panel({ title, actions, className, bodyClassName, children }: PanelProps) {
  return (
    <section className={[styles.panel, className].filter(Boolean).join(" ")}>
      {title !== undefined ? (
        <header className={styles.panelHeader}>
          <span style={{ flex: 1 }}>{title}</span>
          {actions}
        </header>
      ) : null}
      <div className={[styles.panelBody, bodyClassName].filter(Boolean).join(" ")}>{children}</div>
    </section>
  );
}

export function SectionTitle({ children }: { children: ReactNode }) {
  return <h3 className={styles.sectionTitle}>{children}</h3>;
}

// --- Dialog ----------------------------------------------------------------

interface DialogProps {
  title: ReactNode;
  onClose?: () => void;
  footer?: ReactNode;
  size?: "md" | "lg";
  /** When false the dialog has no close affordance; the footer must resolve it. */
  dismissable?: boolean;
  children: ReactNode;
}

export function Dialog({ title, onClose, footer, size = "md", dismissable = true, children }: DialogProps) {
  const titleId = useId();
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && dismissable) onClose?.();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [dismissable, onClose]);

  return (
    <div
      className={styles.backdrop}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && dismissable) onClose?.();
      }}
    >
      <div
        className={styles.dialog}
        data-size={size}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        ref={ref}
      >
        <header className={styles.dialogHeader}>
          <h2 className={styles.dialogTitle} id={titleId}>
            {title}
          </h2>
          {dismissable && onClose ? (
            <IconButton onClick={onClose} aria-label="Close">
              <Icons.close />
            </IconButton>
          ) : null}
        </header>
        <div className={styles.dialogBody}>{children}</div>
        {footer ? <footer className={styles.dialogFooter}>{footer}</footer> : null}
      </div>
    </div>
  );
}

// --- Badge / Notice --------------------------------------------------------

export function Badge({ tone, children }: { tone?: "ok" | "warn" | "danger" | "accent"; children: ReactNode }) {
  return (
    <span className={styles.badge} data-tone={tone}>
      {children}
    </span>
  );
}

export function Notice({ tone, children }: { tone: "warn" | "danger" | "info"; children: ReactNode }) {
  return (
    <div className={styles.notice} data-tone={tone} role={tone === "danger" ? "alert" : undefined}>
      {tone === "info" ? null : <Icons.warning size={14} style={{ flex: "none", marginTop: 2 }} />}
      <span>{children}</span>
    </div>
  );
}
