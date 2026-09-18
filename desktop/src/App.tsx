import { useEffect } from "react";
import { AppProvider } from "./app/AppContext";
import { Shell } from "./components/shell/Shell";
import { useUi } from "./state/ui";
import styles from "./App.module.css";

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
      <Shell onOpenSettings={() => undefined} onOpenUser={() => undefined}>
        <Placeholder view={ui.view} />
      </Shell>
    </AppProvider>
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

function Placeholder({ view }: { view: string }) {
  return <div className={styles.placeholder}>{view}</div>;
}
