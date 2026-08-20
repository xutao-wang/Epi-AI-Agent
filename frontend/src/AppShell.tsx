import { useEffect, useState } from "react";
import type { CSSProperties, ReactNode } from "react";

const DEFAULT_SIDEBAR_WIDTH = 300;
const MIN_SIDEBAR_WIDTH = 260;
const MAX_SIDEBAR_WIDTH = 640;
const SIDEBAR_WIDTH_STORAGE_KEY = "report-agent.sidebar-width";

function clampSidebarWidth(width: number): number {
  return Math.min(MAX_SIDEBAR_WIDTH, Math.max(MIN_SIDEBAR_WIDTH, width));
}

function loadSidebarWidth(): number {
  const savedWidth = window.localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY);
  if (savedWidth === null) {
    return DEFAULT_SIDEBAR_WIDTH;
  }

  const parsedWidth = Number(savedWidth);
  return Number.isFinite(parsedWidth)
    ? clampSidebarWidth(parsedWidth)
    : DEFAULT_SIDEBAR_WIDTH;
}

interface Props {
  sidebar: ReactNode;
  conversation: ReactNode;
  input: ReactNode;
  headerAction?: ReactNode;
}

export default function AppShell({
  sidebar,
  conversation,
  input,
  headerAction,
}: Props) {
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(loadSidebarWidth);
  const shellClassName = isSidebarCollapsed
    ? "report-app-shell sidebar-collapsed"
    : "report-app-shell";

  useEffect(() => {
    window.localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(sidebarWidth));
  }, [sidebarWidth]);

  function beginSidebarResize(event: React.MouseEvent<HTMLDivElement>) {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = sidebarWidth;
    const resize = (moveEvent: MouseEvent) => {
      setSidebarWidth(clampSidebarWidth(startWidth + moveEvent.clientX - startX));
    };
    const stopResize = () => {
      window.removeEventListener("mousemove", resize);
      window.removeEventListener("mouseup", stopResize);
    };

    window.addEventListener("mousemove", resize);
    window.addEventListener("mouseup", stopResize, { once: true });
  }

  return (
    <main
      className={shellClassName}
      style={{ "--sidebar-width": `${sidebarWidth}px` } as CSSProperties}
    >
      <aside className="report-sidebar" aria-label="Saved conversations and tools">
        <button
          aria-label={
            isSidebarCollapsed ? "Show sidebar" : "Hide sidebar"
          }
          className="sidebar-toggle"
          onClick={() => setIsSidebarCollapsed((current) => !current)}
          type="button"
        >
          {isSidebarCollapsed ? ">>" : "<<"}
        </button>
        <div className="report-sidebar-content" hidden={isSidebarCollapsed}>
          {sidebar}
        </div>
      </aside>
      <div
        aria-label="Resize sidebar"
        aria-orientation="vertical"
        aria-valuemax={MAX_SIDEBAR_WIDTH}
        aria-valuemin={MIN_SIDEBAR_WIDTH}
        aria-valuenow={sidebarWidth}
        className="sidebar-resize-handle"
        onMouseDown={beginSidebarResize}
        role="separator"
      />
      <section className="report-main">
        <header className="report-header">
          <h1>Epidemiology Research Agent</h1>
          {headerAction}
        </header>
        <section className="conversation-section" aria-label="Conversation">
          {conversation}
        </section>
        <section className="input-section">{input}</section>
      </section>
    </main>
  );
}
