// A render error must never leave an operator looking at a blank window with
// an instrument running. The measurement lives in the backend process and is
// unaffected; what the operator needs is to know that, to see what broke, and
// a way back to the Stop button. Two boundaries use this: one around the whole
// app, one around the settings dialog body so a broken section closes with the
// dialog instead of taking the measurement view with it.

import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button, Notice } from "./ui";

interface Props {
  children: ReactNode;
  /** Where this boundary sits, for the message: "the settings dialog". */
  scope: string;
  /** Offered instead of a reload when the parent can recover (close the dialog). */
  onDismiss?: (() => void) | undefined;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error(`render error in ${this.props.scope}`, error, info.componentStack);
  }

  render(): ReactNode {
    const { error } = this.state;
    if (error === null) return this.props.children;
    return (
      <div role="alert" style={{ padding: "1rem", display: "grid", gap: "0.75rem" }}>
        <Notice tone="danger">
          Display error in {this.props.scope}: {error.message}. A running measurement is unaffected: it lives in the
          backend, not in this window.
        </Notice>
        <div style={{ display: "flex", gap: "0.5rem" }}>
          {this.props.onDismiss ? (
            <Button variant="primary" onClick={this.props.onDismiss}>
              Close
            </Button>
          ) : null}
          <Button onClick={() => window.location.reload()}>Reload the window</Button>
        </div>
      </div>
    );
  }
}
