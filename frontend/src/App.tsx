import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { connectLedgerEvents, type EventStreamStatus } from "./lib/events";
import type { ConsoleCapabilities, LedgerEvent } from "./lib/types";
import { api } from "./lib/api";
import { LedgerPage } from "./pages/LedgerPage";
import { OverviewPage } from "./pages/OverviewPage";
import { ReconciliationPage } from "./pages/ReconciliationPage";

type View = "overview" | "ledger" | "reconciliation";

const NAVIGATION: ReadonlyArray<{ view: View; label: string; index: string }> = [
  { view: "overview", label: "Control totals", index: "01" },
  { view: "ledger", label: "Journal", index: "02" },
  { view: "reconciliation", label: "Reconciliation", index: "03" }
];

function viewFromHash(): View {
  const hash = globalThis.location.hash.replace(/^#\/?/, "");
  return NAVIGATION.some((item) => item.view === hash) ? hash as View : "overview";
}

function Brand() {
  return (
    <a className="brand" href="#overview" aria-label="LedgerLite overview">
      <span className="brand__text">
        <strong>LedgerLite</strong>
        <small>Financial control // AED</small>
      </span>
    </a>
  );
}

function PrimaryNavigation({
  view
}: {
  view: View;
}) {
  const activeLinkRef = useRef<HTMLAnchorElement>(null);
  useEffect(() => {
    activeLinkRef.current?.scrollIntoView({
      behavior: "auto",
      block: "nearest",
      inline: "center"
    });
  }, [view]);
  return (
    <nav className="primary-nav" aria-label="Console">
      {NAVIGATION.map((item) => (
        <a
          key={item.view}
          ref={view === item.view ? activeLinkRef : undefined}
          className="primary-nav__item"
          href={`#${item.view}`}
          aria-current={view === item.view ? "page" : undefined}
        >
          <span className="primary-nav__index" aria-hidden="true">{item.index}</span>
          <span>{item.label}</span>
        </a>
      ))}
    </nav>
  );
}

export function App() {
  const [view, setView] = useState<View>(viewFromHash);
  const [apiReady, setApiReady] = useState<boolean | null>(null);
  const [capabilities, setCapabilities] = useState<ConsoleCapabilities | null>(null);
  const [eventStatus, setEventStatus] = useState<EventStreamStatus>("idle");
  const [refreshSignal, setRefreshSignal] = useState(0);
  const [lastEvent, setLastEvent] = useState<LedgerEvent | null>(null);
  const mainRef = useRef<HTMLElement>(null);

  const checkReadiness = useCallback(async () => {
    try {
      const [, discovered] = await Promise.all([api.readiness(), api.capabilities()]);
      setCapabilities(discovered);
      setApiReady(true);
    } catch {
      setApiReady(false);
    }
  }, []);

  useEffect(() => {
    if (!globalThis.location.hash) globalThis.history.replaceState(null, "", "#overview");
    const onHashChange = () => {
      setView(viewFromHash());
      globalThis.requestAnimationFrame(() => mainRef.current?.focus({ preventScroll: true }));
    };
    globalThis.addEventListener("hashchange", onHashChange);
    return () => globalThis.removeEventListener("hashchange", onHashChange);
  }, []);

  useEffect(() => {
    void checkReadiness();
    const interval = globalThis.setInterval(() => void checkReadiness(), 30_000);
    return () => globalThis.clearInterval(interval);
  }, [checkReadiness]);

  useEffect(() => {
    const stream = connectLedgerEvents({
      onStatus: setEventStatus,
      onEvent: (event) => {
        setLastEvent(event);
        setRefreshSignal((current) => current + 1);
      },
      onError: () => undefined
    });
    return () => stream.close();
  }, []);

  const eventReady = eventStatus === "open";
  const page = useMemo(() => {
    switch (view) {
      case "ledger":
        return <LedgerPage refreshSignal={refreshSignal} />;
      case "reconciliation":
        return <ReconciliationPage refreshSignal={refreshSignal} />;
      case "overview":
        return (
          <OverviewPage
            refreshSignal={refreshSignal}
            apiReady={apiReady}
            eventReady={eventReady}
            lastEvent={lastEvent}
          />
        );
    }
  }, [apiReady, eventReady, lastEvent, refreshSignal, view]);

  return (
    <div className="app-shell">
      <a className="skip-link" href="#console-main">Skip to console</a>
      <aside className="rail">
        <div className="mobile-header">
          <Brand />
        </div>
        <Brand />
        <PrimaryNavigation view={view} />
        <div className="rail__footer">
          <div className="sandbox-card">
            <div className="sandbox-card__label">Usage restriction</div>
            <p>No authentication. No real funds. Local use only.</p>
            <div className="sandbox-links">
              {capabilities?.documentation ? <a href="/docs" target="_blank" rel="noreferrer">API reference ↗</a> : null}
              <a href="/livez" target="_blank" rel="noreferrer">Liveness ↗</a>
            </div>
          </div>
        </div>
      </aside>

      <div className="workspace">
        <header className="workspace__topbar">
          <span className="topbar__label">LEDGER ADMIN :: OPERATIONS NODE 01</span>
          <div className="connection-group" aria-label="System connections">
            <span className={`connection-item ${apiReady ? "connection-item--ready" : apiReady === false ? "connection-item--warning" : ""}`}>
              <span className="connection-item__dot" aria-hidden="true" />
              {apiReady === null ? "Checking API" : apiReady ? "API ready" : "API unavailable"}
            </span>
            <span className={`connection-item ${eventReady ? "connection-item--ready" : "connection-item--warning"}`}>
              <span className="connection-item__dot" aria-hidden="true" />
              {eventReady ? "Live updates" : eventStatus === "connecting" ? "Connecting" : "Reconnecting"}
            </span>
          </div>
        </header>

        <main id="console-main" ref={mainRef} className="console-main" tabIndex={-1}>
          <span id="overview" className="view-route-target" tabIndex={-1} />
          <span id="ledger" className="view-route-target" tabIndex={-1} />
          <span id="reconciliation" className="view-route-target" tabIndex={-1} />
          {page}
        </main>
      </div>
    </div>
  );
}
