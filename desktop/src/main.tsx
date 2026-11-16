import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ShellOverlays } from "./shell/ShellOverlays";
import "./styles/global.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <ErrorBoundary scope="the application">
      <App />
    </ErrorBoundary>
    <ShellOverlays />
  </React.StrictMode>,
);
