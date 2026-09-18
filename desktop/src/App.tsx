import { useEffect, useState } from "react";
import { AppProvider } from "./app/AppContext";
import { Shell } from "./components/shell/Shell";
import { UserPicker } from "./components/dialogs/UserPicker";
import { PromptDialog } from "./components/dialogs/PromptDialog";
import { ContinuousView } from "./views/continuous/ContinuousView";
import { useSession } from "./state/session";
import { useUi } from "./state/ui";
import styles from "./App.module.css";

type DialogName = "user" | "settings" | null;

export default function App() {
  const ui = useUi();

  useEffect(() => {
    const root = document.documentElement;
    if (ui.theme === "system") {
      const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = prefersDark ? "dark" : "light";
    } else {
      root.dataset.theme = ui.theme;
    }
  }, [ui.theme]);

  return (
    <AppProvider fallback={({ error }) => <Startup error={error} />}>
      <Workspace />
    </AppProvider>
  );
}

function Workspace() {
  const ui = useUi();
  const session = useSession();
  const [dialog, setDialog] = useState<DialogName>(null);

  // No operator, no measuring: the picker opens itself until one is chosen.
  const showUserPicker = dialog === "user" || ui.username === null;
  // A prompt the run is blocked on outranks anything the operator opened.
  const prompt = session.status?.pending_prompt ?? null;

  return (
    <>
      <Shell onOpenSettings={() => setDialog("settings")} onOpenUser={() => setDialog("user")}>
        <View view={ui.view} />
      </Shell>
      {prompt ? <PromptDialog prompt={prompt} /> : showUserPicker ? <UserPicker onClose={() => setDialog(null)} /> : null}
    </>
  );
}

function Startup({ error }: { error: string | null }) {
  return (
    <div className={styles.startup}>
      <div className={styles.startupCard} data-error={error !== null}>
        {error === null ? (
          <>
            <div className={styles.spinner} />
            <div>Starting the measurement backend…</div>
          </>
        ) : (
          <>
            <div className={styles.startupTitle}>The measurement backend did not start</div>
            <pre className={styles.startupError}>{error}</pre>
          </>
        )}
      </div>
    </div>
  );
}

function View({ view }: { view: string }) {
  switch (view) {
    case "resistance":
    case "source_v":
    case "source_i":
    case "four_point":
      // One component per continuous mode instance, keyed so switching modes
      // remounts the plot with the right traces.
      return <ContinuousView key={view} mode={view} />;
    default:
      return <div className={styles.placeholder}>{view}</div>;
  }
}
