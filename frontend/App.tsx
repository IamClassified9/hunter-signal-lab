
import { useEffect, useMemo, useState } from "react";
import "./App.css";

const API_URL = "http://127.0.0.1:8000";

type LiquidityEvent = {
  event_id: string;
  symbol: string;
  timeframe: "5m" | "15m" | "1h" | "4h";
  event_type:
    | "SWING_HIGH_SWEEP"
    | "SWING_LOW_SWEEP"
    | "LIQUIDITY_GRAB"
    | "BREAKOUT"
    | "FAILED_BREAKOUT";
  price: number;
  volume_ratio: number;
  structure: string;
  timestamp: string;
};

const formatEvent = (value: string) =>
  value.replaceAll("_", " ").toLowerCase();

const formatPrice = (value: number) =>
  value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

const formatTime = (value: string) =>
  new Date(value).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

function App() {
  const [events, setEvents] = useState<LiquidityEvent[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [filter, setFilter] = useState("ALL");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();

    async function loadEvents() {
      try {
        setLoading(true);
        setError("");

        const response = await fetch(`${API_URL}/api/events`, {
          signal: controller.signal,
        });

        if (!response.ok) {
          throw new Error(`API returned HTTP ${response.status}`);
        }

        const data: LiquidityEvent[] = await response.json();

        setEvents(data);
        setSelectedId((current) =>
          data.some((event) => event.event_id === current)
            ? current
            : data[0]?.event_id ?? ""
        );
      } catch (err) {
        if (err instanceof Error && err.name === "AbortError") return;

        setError(
          err instanceof Error
            ? err.message
            : "Could not load market events."
        );
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }

    loadEvents();

    return () => controller.abort();
  }, []);

  const filteredEvents = useMemo(() => {
    const query = search.trim().toLowerCase();

    return events.filter((event) => {
      const matchesType =
        filter === "ALL" || event.event_type === filter;

      const matchesSearch = [
        event.event_id,
        event.symbol,
        event.event_type,
        event.structure,
      ]
        .join(" ")
        .toLowerCase()
        .includes(query);

      return matchesType && matchesSearch;
    });
  }, [events, filter, search]);

  const selectedEvent =
    filteredEvents.find((event) => event.event_id === selectedId) ??
    filteredEvents[0];

  const symbols = new Set(events.map((event) => event.symbol)).size;
  const sweeps = events.filter((event) =>
    event.event_type.includes("SWEEP")
  ).length;
  const breakouts = events.filter((event) =>
    event.event_type.includes("BREAKOUT")
  ).length;

  const eventTypes = [
    "ALL",
    ...Array.from(new Set(events.map((event) => event.event_type))),
  ];

  function downloadReport() {
    const report = {
      application: "Hunter Signal Lab",
      generated_at: new Date().toISOString(),
      source: "FastAPI event endpoint",
      event_count: filteredEvents.length,
      events: filteredEvents,
      note: "Event observations only. No AI predictions are included.",
    };

    const blob = new Blob(
      [JSON.stringify(report, null, 2)],
      { type: "application/json" }
    );

    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");

    link.href = url;
    link.download = "hunter-events-report.json";
    link.click();

    URL.revokeObjectURL(url);
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#">
          <span className="brand-mark">H</span>
          <span>
            <strong>HUNTER</strong>
            <small>SIGNAL LAB</small>
          </span>
        </a>

        <div className="nav-label">WORKSPACE</div>

        <button className="nav-item active">
          <span>◫</span> Overview
        </button>
        <button
          className="nav-item"
          onClick={() =>
            document.getElementById("event-explorer")?.scrollIntoView({
              behavior: "smooth",
            })
          }
        >
          <span>⌁</span> Event explorer
        </button>
        <button
          className="nav-item"
          onClick={() =>
            document.getElementById("prediction-panel")?.scrollIntoView({
              behavior: "smooth",
            })
          }
        >
          <span>◎</span> AI predictions
        </button>

        <div className="sidebar-bottom">
          <div className="model-card">
            <div className="model-heading">
              <span className="status-dot" />
              SYSTEM STATUS
            </div>
            <strong>{loading ? "Connecting..." : error ? "Offline" : "Connected"}</strong>
            <p>
              {error ? "API connection failed" : "Local research environment"}
            </p>
          </div>
          <div className="sidebar-footer">HUNTER SIGNAL LAB · v0.1.0</div>
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <div className="breadcrumb">
            Workspace <span>/</span> <strong>Overview</strong>
          </div>
          <div className="topbar-right">
            <span className="live-indicator">
              <span className="status-dot" />
              API {error ? "DISCONNECTED" : loading ? "CONNECTING" : "CONNECTED"}
            </span>
            <div className="avatar">H</div>
          </div>
        </header>

        <section className="page-content">
          <div className="page-heading">
            <div>
              <div className="eyebrow">MARKET INTELLIGENCE / OVERVIEW</div>
              <h1>Signal Overview<span>.</span></h1>
              <p>
                Understand liquidity events. Review the evidence. Stay focused.
              </p>
            </div>
            <button
              className="primary-button"
              onClick={downloadReport}
              disabled={loading || !!error || filteredEvents.length === 0}
            >
              ↓ Export report
            </button>
          </div>

          {error && (
            <div className="error-banner">
              <strong>Unable to connect to the backend.</strong>
              <span>{error}. Check that FastAPI is running on port 8000.</span>
              <button onClick={() => window.location.reload()}>
                Retry
              </button>
            </div>
          )}

          <div className="metrics-grid">
            <Metric
              label="TOTAL EVENTS"
              value={loading ? "—" : String(events.length).padStart(2, "0")}
              note="Recorded event samples"
              icon="⌁"
            />
            <Metric
              label="MARKETS TRACKED"
              value={loading ? "—" : String(symbols).padStart(2, "0")}
              note="Unique trading pairs"
              icon="◈"
            />
            <Metric
              label="LIQUIDITY SWEEPS"
              value={loading ? "—" : String(sweeps).padStart(2, "0")}
              note="Recorded sweep events"
              icon="↗"
            />
            <Metric
              label="BREAKOUT EVENTS"
              value={loading ? "—" : String(breakouts).padStart(2, "0")}
              note="Breakouts and failures"
              icon="⌁"
            />
          </div>

          <div className="section-heading">
            <div>
              <h2>Market activity</h2>
              <p>Distribution of the recorded event types</p>
            </div>
            <span className="subtle-tag">SAMPLE DATA</span>
          </div>

          <section className="activity-panel">
            {loading ? (
              <p className="empty-state">Loading event activity...</p>
            ) : events.length === 0 ? (
              <p className="empty-state">No events have been recorded.</p>
            ) : (
              <div className="activity-bars">
                {Array.from(new Set(events.map((event) => event.event_type))).map(
                  (type) => {
                    const count = events.filter(
                      (event) => event.event_type === type
                    ).length;

                    return (
                      <div className="activity-row" key={type}>
                        <span className="activity-name">
                          {formatEvent(type)}
                        </span>
                        <div className="bar-track">
                          <div
                            className="bar-fill"
                            style={{
                              width: `${(count / events.length) * 100}%`,
                            }}
                          />
                        </div>
                        <span className="activity-count">{count}</span>
                      </div>
                    );
                  }
                )}
              </div>
            )}
            <div className="panel-footnote">
              Based on the currently loaded event records, not live market activity.
            </div>
          </section>

          <div className="explorer-layout" id="event-explorer">
            <section className="events-section">
              <div className="section-heading">
                <div>
                  <h2>Event explorer</h2>
                  <p>Inspect recorded liquidity events and market structure.</p>
                </div>
                <span className="subtle-tag">{filteredEvents.length} RECORDS</span>
              </div>

              <div className="filters">
                <div className="search-box">
                  <span>⌕</span>
                  <input
                    aria-label="Search events"
                    placeholder="Search symbol, event or ID..."
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                  />
                </div>

                <select
                  aria-label="Filter event type"
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                >
                  {eventTypes.map((type) => (
                    <option key={type} value={type}>
                      {type === "ALL" ? "All event types" : formatEvent(type)}
                    </option>
                  ))}
                </select>
              </div>

              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>EVENT / MARKET</th>
                      <th>TYPE</th>
                      <th>TIMEFRAME</th>
                      <th>PRICE</th>
                      <th>VOLUME RATIO</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredEvents.map((event) => (
                      <tr
                        key={event.event_id}
                        className={
                          selectedEvent?.event_id === event.event_id
                            ? "selected-row"
                            : ""
                        }
                        onClick={() => setSelectedId(event.event_id)}
                      >
                        <td>
                          <strong>{event.event_id}</strong>
                          <span className="cell-subtitle">{event.symbol}</span>
                        </td>
                        <td>
                          <span className={`event-badge ${event.event_type.toLowerCase()}`}>
                            {formatEvent(event.event_type)}
                          </span>
                        </td>
                        <td>{event.timeframe}</td>
                        <td className="price-cell">
                          {formatPrice(event.price)}
                        </td>
                        <td>{event.volume_ratio.toFixed(1)}×</td>
                      </tr>
                    ))}
                    {!loading && filteredEvents.length === 0 && (
                      <tr>
                        <td colSpan={5} className="empty-state">
                          No events match your filters.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </section>

            <aside className="detail-panel">
              <div className="detail-header">
                <span className="eyebrow">SELECTED EVENT</span>
                <span className="detail-symbol">◈</span>
              </div>

              {selectedEvent ? (
                <>
                  <h3>{selectedEvent.symbol}</h3>
                  <p className="detail-id">{selectedEvent.event_id}</p>
                  <span className="event-badge">
                    {formatEvent(selectedEvent.event_type)}
                  </span>

                  <div className="detail-price">
                    <span>Recorded price</span>
                    <strong>{formatPrice(selectedEvent.price)}</strong>
                  </div>

                  <div className="detail-fields">
                    <DetailField label="TIMEFRAME" value={selectedEvent.timeframe} />
                    <DetailField
                      label="VOLUME RATIO"
                      value={`${selectedEvent.volume_ratio.toFixed(1)}×`}
                    />
                    <DetailField
                      label="MARKET STRUCTURE"
                      value={formatEvent(selectedEvent.structure)}
                    />
                    <DetailField
                      label="TIMESTAMP"
                      value={formatTime(selectedEvent.timestamp)}
                    />
                  </div>

                  <div className="evidence-note">
                    <strong>Evidence record</strong>
                    <p>
                      This panel displays supplied event attributes. It does
                      not establish a trade outcome or predict future prices.
                    </p>
                  </div>
                </>
              ) : (
                <p className="empty-state">Select an event to inspect it.</p>
              )}
            </aside>
          </div>

          <section className="prediction-panel" id="prediction-panel">
            <div className="prediction-icon">◎</div>
            <div className="prediction-copy">
              <span className="eyebrow">MACHINE LEARNING</span>
              <h2>TabPFN prediction engine</h2>
              <p>
                The event dashboard is connected. Predictive inference is not
                yet configured because we still need labeled historical examples
                and a validated prediction target.
              </p>
            </div>
            <span className="model-status">NOT CONNECTED</span>
          </section>

          <footer className="page-footer">
            <span>HUNTER SIGNAL LAB</span>
            <span>Research tooling · Not financial advice</span>
          </footer>
        </section>
      </main>
    </div>
  );
}

function Metric({
  label,
  value,
  note,
  icon,
}: {
  label: string;
  value: string;
  note: string;
  icon: string;
}) {
  return (
    <div className="metric-card">
      <div className="metric-top">
        <span>{label}</span>
        <span className="metric-icon">{icon}</span>
      </div>
      <strong className="metric-value">{value}</strong>
      <span className="metric-note">{note}</span>
    </div>
  );
}

function DetailField({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div className="detail-field">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export default App;
