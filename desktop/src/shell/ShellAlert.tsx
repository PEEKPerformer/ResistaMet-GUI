// A blocking alert owned by the shell layer. It sits above every dialog the
// app can open and cannot be dismissed except through its buttons: what it
// says concerns the instrument, not the screen.

import { useEffect, useId, useRef, type ReactNode } from "react";
import styles from "./shell.module.css";

interface ShellAlertProps {
  title: string;
  tone?: "danger" | "neutral";
  children: ReactNode;
  /** Buttons, in reading order. */
  actions: ReactNode;
}

export function ShellAlert({ title, tone = "neutral", children, actions }: ShellAlertProps) {
  const titleId = useId();
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Focus goes to the button marked as the default, else to the alert.
    const preferred = ref.current?.querySelector<HTMLElement>("[data-default]");
    (preferred ?? ref.current)?.focus();
  }, []);

  return (
    <div className={styles.backdrop}>
      <div
        className={styles.alert}
        data-tone={tone}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        ref={ref}
      >
        <h2 className={styles.title} id={titleId}>
          {title}
        </h2>
        <div className={styles.body}>{children}</div>
        <div className={styles.actions}>{actions}</div>
      </div>
    </div>
  );
}

interface ShellButtonProps {
  onClick: () => void;
  disabled?: boolean;
  variant?: "default" | "primary" | "danger";
  /** Takes focus when the alert opens, so Enter chooses it. */
  isDefault?: boolean;
  children: ReactNode;
}

export function ShellButton({ onClick, disabled = false, variant = "default", isDefault = false, children }: ShellButtonProps) {
  return (
    <button
      type="button"
      className={styles.button}
      data-variant={variant}
      data-default={isDefault ? "" : undefined}
      disabled={disabled}
      onClick={onClick}
    >
      {children}
    </button>
  );
}
