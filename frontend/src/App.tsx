import { useCallback, useEffect, useMemo, useState, useRef } from "react";
import "./App.css";
import LiquidityChart from "./components/LiquidityChart";
import {
  getAnalysis,
  getBrief,
  getCandles,
  getEvents,
  getMarkets,
  getSummary,
  searchMarkets,
} from "./services/api";
import type {
  LiquidityEvent,
  Market,
  MarketCandle,
  LiquidityLevel,
  SummaryResponse,
} from "./types/market";
import type { BriefResponse, MarketSearchResult } from "./services/api";

const DEFAULT_LIMIT = 300;
const WATCHLIST_KEY = "hunter.watchlist.v1";
const PRIORITY_WINDOW_MS = 24 * 60 * 60 * 1000;

function readWatchlist(): string[] {
  try {
    const raw = window.localStorage.getItem(WATCHLIST_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return [...new Set(parsed.filter((item): item is string => typeof item === "string"))].slice(0, 20);
  } catch {
    return [];
  }
}

const formatEvent = (value: string) => value.replaceAll("_", " ").toLowerCase();

const formatPrice = (value: number) =>
  value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 6,
  });

const formatUtcTime = (value: string) => {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return value;
  return `${date.toLocaleString("en-GB", {
    timeZone: "UTC",
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  })} UTC`;
};

function App() {
  const [markets, setMarkets] = useState<Market[]>([]);
  const [symbol, setSymbol] = useState("");
  const [timeframe, setTimeframe] = useState("");
  const [events, setEvents] = useState<LiquidityEvent[]>([]);
  const [candles, setCandles] = useState<MarketCandle[]>([]);
  const [levels, setLevels] = useState<LiquidityLevel[]>([]);
  const [summary, setSummary] = useState<SummaryResponse | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [filter, setFilter] = useState("ALL");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [marketsLoading, setMarketsLoading] = useState(true);
  const [error, setError] = useState("");
  const [refreshToken, setRefreshToken] = useState(0);
  const [lastUpdated, setLastUpdated] = useState("");
  const lastSuccessfulFetchAt = useRef<number | null>(null);
  const activeSelection = useRef("");
  const pendingEventId = useRef("");
  const [engineConfig, setEngineConfig] = useState<Record<string, unknown> | null>(null);
  const [watchlist, setWatchlist] = useState<string[]>(readWatchlist);
  const [watchOnly, setWatchOnly] = useState(false);
  const [priorityEvents, setPriorityEvents] = useState<LiquidityEvent[]>([]);
  const [priorityLoadedAt, setPriorityLoadedAt] = useState(0);
  const [priorityError, setPriorityError] = useState("");
  const [pairSearch, setPairSearch] = useState("");
  const [pairSearchResults, setPairSearchResults] = useState<MarketSearchResult[]>([]);
  const [pairSearchLoading, setPairSearchLoading] = useState(false);
  const [pairSearchError, setPairSearchError] = useState("");
  const [brief, setBrief] = useState<BriefResponse | null>(null);
  const [briefLoading, setBriefLoading] = useState(false);
  const [briefError, setBriefError] = useState("");

  const refresh = useCallback(() => {
    setRefreshToken((current) => current + 1);
  }, []);

  const createBrief = useCallback(async () => {
    setBriefLoading(true);
    setBriefError("");
    try {
      const result = await getBrief(watchlist);
      setBrief(result);
    } catch (error: unknown) {
      setBriefError(error instanceof Error ? error.message : "Could not create the brief.");
    } finally {
      setBriefLoading(false);
    }
  }, [watchlist]);

  useEffect(() => {
    const controller = new AbortController();

    async function loadInitialData() {
      setMarketsLoading(true);
      try {
        const [marketData, summaryData] = await Promise.all([
          getMarkets(controller.signal),
          getSummary(controller.signal),
        ]);
        setMarkets(marketData);
        setSummary(summaryData);
        const firstMarket = marketData[0];
        if (firstMarket) {
          setSymbol((current) => current || firstMarket.symbol);
          setTimeframe((current) => current || firstMarket.timeframes[0] || "");
        }
      } catch (error: unknown) {
        if (error instanceof Error && error.name === "AbortError") return;
        setError(error instanceof Error ? error.message : "Could not load markets.");
      } finally {
        if (!controller.signal.aborted) setMarketsLoading(false);
      }
    }

    void loadInitialData();
    return () => controller.abort();
  }, []);

  const availableTimeframes = useMemo(
    () => markets.find((market) => market.symbol === symbol)?.timeframes ?? ["5m", "15m", "1h", "4h"],
    [markets, symbol],
  );

  useEffect(() => {
    if (!symbol || !timeframe) return;

    const controller = new AbortController();
    async function loadSelectedMarket() {
      setLoading(true);
      setError("");
      try {
        const selectionKey = `${symbol}::${timeframe}`;
        if (activeSelection.current !== selectionKey) {
          activeSelection.current = selectionKey;
          setCandles([]);
          setLevels([]);
          setEvents([]);
          setSelectedId("");
        }

        const [analysis, candleResponse, summaryData] = await Promise.all([
          getAnalysis(symbol, timeframe, DEFAULT_LIMIT, controller.signal),
          getCandles(symbol, timeframe, DEFAULT_LIMIT, controller.signal),
          getSummary(controller.signal),
        ]);
        const eventData = [...analysis.events].sort((left, right) => {
          const detectedOrder = (right.detected_at ?? "").localeCompare(left.detected_at ?? "");
          return detectedOrder || right.event_id.localeCompare(left.event_id);
        });
        setLevels(analysis.levels);
        setCandles(candleResponse.candles);
        setEvents(eventData);
        setSummary(summaryData);
        setEngineConfig(analysis.config);
        const wantedId = pendingEventId.current;
        pendingEventId.current = "";
        setSelectedId((current) => {
          if (wantedId && eventData.some((event) => event.event_id === wantedId)) {
            return wantedId;
          }
          return eventData.some((event) => event.event_id === current)
            ? current
            : eventData[0]?.event_id ?? "";
        });
        const successTime = Date.now();
        lastSuccessfulFetchAt.current = successTime;
        setLastUpdated(new Date(successTime).toISOString());
      } catch (error: unknown) {
        if (error instanceof Error && error.name === "AbortError") return;
        setError(error instanceof Error ? error.message : "Could not load market data.");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }

    void loadSelectedMarket();
    return () => controller.abort();
  }, [symbol, timeframe, refreshToken]);

  useEffect(() => {
    try {
      window.localStorage.setItem(WATCHLIST_KEY, JSON.stringify(watchlist));
    } catch {
      // Storage can be unavailable (private mode); the watchlist then lasts for this visit only.
    }
  }, [watchlist]);

  useEffect(() => {
    const controller = new AbortController();

    async function loadPriorityEvents() {
      try {
        let data: LiquidityEvent[];
        if (watchOnly && watchlist.length > 0) {
          const batches: string[][] = [];
          for (let index = 0; index < watchlist.length; index += 10) {
            batches.push(watchlist.slice(index, index + 10));
          }
          const results = await Promise.all(
            batches.map((batch) => getEvents(500, controller.signal, batch)),
          );
          const unique = new Map<string, LiquidityEvent>();
          for (const event of results.flat()) unique.set(event.event_id, event);
          data = [...unique.values()];
        } else {
          data = await getEvents(500, controller.signal);
        }
        setPriorityEvents(data);
        setPriorityLoadedAt(Date.now());
        setPriorityError("");
      } catch (error: unknown) {
        if (error instanceof Error && error.name === "AbortError") return;
        setPriorityError(error instanceof Error ? error.message : "Could not load top events.");
      }
    }

    void loadPriorityEvents();
    return () => controller.abort();
  }, [refreshToken, watchOnly, watchlist]);

  useEffect(() => {
    const query = pairSearch.trim();
    if (query.length < 2) {
      setPairSearchResults([]);
      setPairSearchLoading(false);
      setPairSearchError("");
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setPairSearchLoading(true);
      setPairSearchError("");
      try {
        const results = await searchMarkets(query, 10, controller.signal);
        setPairSearchResults(results);
      } catch (error: unknown) {
        if (error instanceof Error && error.name === "AbortError") return;
        setPairSearchError(error instanceof Error ? error.message : "Could not search markets.");
        setPairSearchResults([]);
      } finally {
        if (!controller.signal.aborted) setPairSearchLoading(false);
      }
    }, 400);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [pairSearch]);

  useEffect(() => {
    const refreshIfDue = () => {
      if (document.visibilityState !== "visible") return;
      const lastSuccess = lastSuccessfulFetchAt.current;
      if (lastSuccess !== null && Date.now() - lastSuccess < 30_000) return;
      setRefreshToken((current) => current + 1);
    };

    const timer = window.setInterval(refreshIfDue, 60_000);
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") refreshIfDue();
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, []);

  useEffect(() => {
    if (!availableTimeframes.includes(timeframe) && availableTimeframes[0]) {
      setTimeframe(availableTimeframes[0]);
    }
  }, [availableTimeframes, timeframe]);

  const topEvents = useMemo(() => {
    const cutoff = priorityLoadedAt - PRIORITY_WINDOW_MS;
    const watched = new Set(watchlist);
    return priorityEvents
      .filter((event) => {
        const known = Date.parse(event.detected_at ?? event.timestamp);
        return (
          Number.isFinite(known) &&
          known >= cutoff &&
          (!watchOnly || watched.has(event.symbol))
        );
      })
      .sort(
        (left, right) =>
          right.volume_ratio - left.volume_ratio ||
          right.event_id.localeCompare(left.event_id),
      )
      .slice(0, 5);
  }, [priorityEvents, priorityLoadedAt, watchlist, watchOnly]);

  const isWatched = watchlist.includes(symbol);
  const watchedMarkets = watchlist;
  const selectableMarkets = useMemo(() => {
    const bySymbol = new Map(markets.map((market) => [market.symbol, market]));
    for (const watched of watchlist) {
      if (!bySymbol.has(watched)) {
        bySymbol.set(watched, { symbol: watched, timeframes: ["5m", "15m", "1h", "4h"], exchange: "binance" });
      }
    }
    return [...bySymbol.values()];
  }, [markets, watchlist]);

  function toggleWatch(target: string) {
    if (watchlist.includes(target)) {
      setWatchlist(watchlist.filter((value) => value !== target));
      return;
    }
    if (watchlist.length >= 20) {
      setPairSearchError("Watchlist limit reached (20 pairs). Remove a pair before adding another.");
      return;
    }
    setWatchlist([...watchlist, target]);
    setPairSearchError("");
  }

  function addSearchResult(result: MarketSearchResult) {
    if (!watchlist.includes(result.symbol) && watchlist.length >= 20) {
      setPairSearchError("Watchlist limit reached (20 pairs). Remove a pair before adding another.");
      return;
    }
    if (!watchlist.includes(result.symbol)) {
      setWatchlist([...watchlist, result.symbol]);
    }
    setSymbol(result.symbol);
    setTimeframe((current) => current || "15m");
    setPairSearchError("");
  }

  function focusEvent(target: LiquidityEvent) {
    setFilter("ALL");
    setSearch("");
    if (target.symbol === symbol && target.timeframe === timeframe) {
      setSelectedId(target.event_id);
    } else {
      pendingEventId.current = target.event_id;
      setSymbol(target.symbol);
      setTimeframe(target.timeframe);
    }
    document.getElementById("event-explorer")?.scrollIntoView({ behavior: "smooth" });
  }

  const filteredEvents = useMemo(() => {
    const query = search.trim().toLowerCase();
    return events.filter((event) => {
      const matchesType = filter === "ALL" || event.event_type === filter;
      const matchesSearch = [
        event.event_id,
        event.symbol,
        event.event_type,
        event.structure,
      ].join(" ").toLowerCase().includes(query);
      return matchesType && matchesSearch;
    });
  }, [events, filter, search]);

  const selectedEvent =
    filteredEvents.find((event) => event.event_id === selectedId) ?? filteredEvents[0];

  const symbolsCount = summary?.symbols.length ?? markets.length;
  const sweeps = events.filter((event) => event.event_type.toUpperCase().includes("SWEEP")).length;
  const breakouts = events.filter((event) =>
    event.event_type.toUpperCase().includes("BREAKOUT"),
  ).length;
  const eventTypes = [
    "ALL",
    ...Array.from(new Set(events.map((event) => event.event_type))),
  ];

  function downloadReport() {
    const report = {
      application: "Hunter Signal Lab",
      generated_at: new Date().toISOString(),
      source: "Analysis response for selected market and timeframe",
      symbol,
      timeframe,
      data_source: summary?.data_source ?? "unknown",
      engine_config: engineConfig,
      times_are: "UTC; timestamp is the candle open, detected_at is when the event became knowable",
      event_count: filteredEvents.length,
      events: filteredEvents,
      liquidity_brief: brief ? {
        generated_at: brief.generated_at,
        model: brief.model,
        source: brief.source,
        summary: brief.summary,
        events: brief.events.map((item) => ({
          event_id: item.event_id,
          evidence: item.evidence,
          sentences: item.sentences,
          chart_snapshot_data_uri: item.snapshot ?? null,
        })),
      } : null,
      note: "Event observations only. No AI predictions are included.",
    };
    const blob = new Blob([JSON.stringify(report, null, 2)], {
      type: "application/json",
    });
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
        <button className="nav-item active" onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}>
          <span>◫</span> Overview
        </button>
        <button className="nav-item" onClick={() => document.getElementById("priority-panel")?.scrollIntoView({ behavior: "smooth" })}>
          <span>★</span> Top 5 &amp; watchlist
        </button>
        <button className="nav-item" onClick={() => document.getElementById("event-explorer")?.scrollIntoView({ behavior: "smooth" })}>
          <span>⌁</span> Event explorer
        </button>
        <button className="nav-item" onClick={() => document.getElementById("prediction-panel")?.scrollIntoView({ behavior: "smooth" })}>
          <span>◎</span> AI predictions
        </button>
        <div className="sidebar-bottom">
          <div className="model-card">
            <div className="model-heading"><span className="status-dot" />SYSTEM STATUS</div>
            <strong>{loading || marketsLoading ? "Connecting..." : error ? "Offline" : "Connected"}</strong>
            <p>{error ? "API connection failed" : "Local research environment"}</p>
          </div>
          <div className="sidebar-footer">HUNTER SIGNAL LAB · v0.1.0</div>
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <div className="breadcrumb">Workspace <span>/</span> <strong>Overview</strong></div>
          <div className="topbar-right">
            <span className="live-indicator"><span className="status-dot" />API {error ? "DISCONNECTED" : loading || marketsLoading ? "CONNECTING" : "CONNECTED"}</span>
            <div className="avatar">H</div>
          </div>
        </header>

        <section className="page-content">
          <div className="page-heading">
            <div>
              <div className="eyebrow">MARKET INTELLIGENCE / OVERVIEW</div>
              <h1>Signal Overview<span>.</span></h1>
              <p>Understand liquidity events. Review the evidence. Stay focused.</p>
            </div>
            <div className="heading-actions">
              <button className="primary-button" onClick={() => void createBrief()} disabled={briefLoading || loading}>{briefLoading ? "Creating brief…" : "✦ Brief"}</button>
              <button className="secondary-button" onClick={refresh} disabled={loading}>↻ Refresh</button>
              <button className="primary-button" onClick={downloadReport} disabled={loading || !!error || filteredEvents.length === 0}>↓ Export report</button>
            </div>
          </div>

          {error && (
            <div className="error-banner" role="alert">
              <strong>Unable to load backend data.</strong>
              <span>{error}</span>
              <button onClick={refresh}>Retry</button>
            </div>
          )}

          <section className="brief-panel" aria-labelledby="brief-title">
            <div className="brief-heading">
              <div><div className="eyebrow">PLAIN-ENGLISH EVENT BRIEF</div><h2 id="brief-title">Your liquidity brief</h2><p>Two sentences per event, with the engine evidence shown beside them.</p></div>
              {brief && <span className={`brief-source ${brief.source === "gemma" ? "brief-source-ai" : ""}`}>{brief.source === "gemma" ? brief.model : "TEMPLATE FALLBACK"}</span>}
            </div>
            {briefError && <p className="brief-error" role="alert">{briefError}</p>}
            {!brief && !briefLoading && <p className="empty-state">Select <strong>Brief</strong> to summarize the top three events from your watchlist, or configured markets if your watchlist is empty.</p>}
            {briefLoading && <p className="empty-state">Reading engine events and preparing your brief…</p>}
            {brief && !briefLoading && <>
              <div className="brief-summary"><strong>Overall summary</strong>{brief.summary.map((line, index) => <p key={`${index}-${line}`}>{line}</p>)}</div>
              {brief.events.length === 0 ? <p className="empty-state">No events were found for this watchlist.</p> : <div className="brief-events">
                {brief.events.map((item, index) => <article className="brief-event" key={item.event_id ?? index}>
                  <div className="brief-event-title"><span>{index + 1}</span><div><strong>{item.evidence.symbol} · {item.evidence.timeframe}</strong><small>{String(item.evidence.event_type ?? "EVENT").replaceAll("_", " ")}</small></div></div>
                  <div className="brief-evidence">
                    <span>Event price <strong>{typeof item.evidence.price === "number" ? formatPrice(item.evidence.price) : "—"}</strong></span>
                    <span>Linked level <strong>{typeof item.evidence.level_price === "number" ? formatPrice(item.evidence.level_price) : "Not supplied"}</strong></span>
                    <span>Volume ratio <strong>{typeof item.evidence.volume_ratio === "number" ? `${item.evidence.volume_ratio.toFixed(2)}×` : "Not supplied"}</strong></span>
                    <span>Structure <strong>{item.evidence.structure ?? "Not supplied"}</strong></span>
                    <span>Level status <strong>{item.evidence.level_swept === true ? "Swept" : item.evidence.level_swept === false ? "Unswept" : "Not supplied"}</strong></span>
                    <span>Event time <strong>{item.evidence.timestamp ? formatUtcTime(item.evidence.timestamp) : "Not supplied"}</strong></span>
                    <span>Detected <strong>{item.evidence.detected_at ? formatUtcTime(item.evidence.detected_at) : "Not supplied"}</strong></span>
                  </div>
                  {item.snapshot && <img className="brief-snapshot" src={item.snapshot} alt={item.snapshot_alt ?? "Chart snapshot with event and linked liquidity level marked"} loading="lazy" />}
                  <ol className="brief-sentences">{item.sentences.map((sentence, sentenceIndex) => <li key={`${sentenceIndex}-${sentence}`}>{sentence}</li>)}</ol>
                </article>)}
              </div>}
              {brief.source === "template" && brief.fallback_reason && <p className="brief-error" role="status">Gemma failed; template fallback is enabled. Reason: {brief.fallback_reason}</p>}
              <p className="brief-disclaimer">{brief.disclaimer} · Source: {brief.model}</p>
            </>}
          </section>

          <div className="metrics-grid">
            <Metric label="TOTAL EVENTS" value={loading ? "—" : String(summary?.total_events ?? events.length).padStart(2, "0")} note="All markets, 15m and 1h only" icon="⌁" />
            <Metric label="MARKETS TRACKED" value={loading ? "—" : String(symbolsCount).padStart(2, "0")} note="Unique trading pairs" icon="◈" />
            <Metric label="LIQUIDITY SWEEPS" value={loading ? "—" : String(sweeps).padStart(2, "0")} note="Selected market and timeframe" icon="↗" />
            <Metric label="BREAKOUT EVENTS" value={loading ? "—" : String(breakouts).padStart(2, "0")} note="Breakouts and failures" icon="⌁" />
          </div>

          <section className="priority-section" id="priority-panel">
            <div className="section-heading">
              <div><h2>Top 5 events, last 24 hours</h2><p>Ranked by volume ratio only. An event-priority list, not a prediction or a trade signal.</p></div>
              <label className="watch-only-toggle">
                <input type="checkbox" checked={watchOnly} onChange={(event) => setWatchOnly(event.currentTarget.checked)} />
                Watchlist only
              </label>
            </div>
            <div className="priority-panel">
              {priorityError ? <p className="empty-state">{priorityError}</p> : topEvents.length === 0 ? (
                <p className="empty-state">{watchOnly && watchlist.length === 0 ? "Your watchlist is empty. Add a coin with the star button in Market selection." : "No events in the last 24 hours for this view."}</p>
              ) : (
                <ol className="priority-list">
                  {topEvents.map((event, index) => (
                    <li key={event.event_id}>
                      <button className="priority-item" onClick={() => focusEvent(event)}>
                        <span className="priority-rank">{index + 1}</span>
                        <span className="priority-main"><strong>{event.symbol}</strong><small>{event.timeframe} · {formatEvent(event.event_type)}</small></span>
                        <span className="priority-meta"><strong>{event.volume_ratio.toFixed(2)}×</strong><small>{formatUtcTime(event.detected_at ?? event.timestamp)}</small></span>
                      </button>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          </section>

          <div className="section-heading">
            <div><h2>Market activity</h2><p>Distribution of the selected market's recorded event types</p></div>
            <span className="subtle-tag">{summary?.data_source ?? "API DATA"}</span>
          </div>
          <section className="activity-panel">
            {loading ? <p className="empty-state">Loading event activity...</p> : events.length === 0 ? <p className="empty-state">No events have been recorded for this selection.</p> : (
              <div className="activity-bars">
                {Array.from(new Set(events.map((event) => event.event_type))).map((type) => {
                  const count = events.filter((event) => event.event_type === type).length;
                  return <div className="activity-row" key={type}>
                    <span className="activity-name">{formatEvent(type)}</span>
                    <div className="bar-track"><div className="bar-fill" style={{ width: `${(count / events.length) * 100}%` }} /></div>
                    <span className="activity-count">{count}</span>
                  </div>;
                })}
              </div>
            )}
            <div className="panel-footnote">
              {lastUpdated ? `Last updated ${formatUtcTime(lastUpdated)}. Times are UTC.` : "Waiting for the first successful data refresh."}
            </div>
          </section>

          <section className="market-section">
            <div className="section-heading">
              <div><h2>Market selection</h2><p>Choose the market and timeframe to inspect.</p></div>
              <span className="subtle-tag">CANDLE LIMIT {DEFAULT_LIMIT}</span>
            </div>
            <div className="market-controls">
              <label>Symbol
                <select value={symbol} onChange={(event) => setSymbol(event.currentTarget.value)} disabled={marketsLoading || selectableMarkets.length === 0}>
                  {selectableMarkets.map((market) => <option key={market.symbol} value={market.symbol}>{`${watchlist.includes(market.symbol) ? "★ " : ""}${market.symbol} · ${market.exchange}`}</option>)}
                </select>
              </label>
              <label>Timeframe
                <select value={timeframe} onChange={(event) => setTimeframe(event.currentTarget.value)} disabled={!symbol || availableTimeframes.length === 0}>
                  {availableTimeframes.map((value) => <option key={value} value={value}>{value}</option>)}
                </select>
              </label>
              <span className="selection-status">{marketsLoading ? "Loading markets…" : symbol && timeframe ? `${symbol} / ${timeframe}` : "No markets available"}</span>
            </div>
            <div className="pair-search-panel">
              <label htmlFor="pair-search-input">Search Binance USDT spot pairs</label>
              <input id="pair-search-input" value={pairSearch} onChange={(event) => setPairSearch(event.currentTarget.value)} placeholder="Search base coin, e.g. SOL" autoComplete="off" />
              <p className="pair-search-hint">Enter 2–20 letters or digits. Search waits 400 ms after typing.</p>
              {pairSearchLoading && <p className="pair-search-hint">Searching active spot markets…</p>}
              {pairSearchError && <p className="pair-search-error" role="alert">{pairSearchError}</p>}
              {pairSearchResults.length > 0 && <ul className="pair-search-results">{pairSearchResults.map((result) => <li key={result.symbol}><span><strong>{result.base}</strong><small>{result.symbol}</small></span><button onClick={() => addSearchResult(result)} disabled={watchlist.includes(result.symbol)}>{watchlist.includes(result.symbol) ? "Added" : "Add to watchlist"}</button></li>)}</ul>}
              {pairSearch.trim().length >= 2 && !pairSearchLoading && !pairSearchError && pairSearchResults.length === 0 && <p className="pair-search-hint">No matching active Binance USDT spot pairs.</p>}
            </div>
            <div className="watchlist-row">
              <button className={`star-button${isWatched ? " active" : ""}`} onClick={() => toggleWatch(symbol)} disabled={!symbol} aria-pressed={isWatched}>
                {isWatched ? "★ In watchlist" : "☆ Add to watchlist"}
              </button>
              {watchedMarkets.map((value) => (
                <span className="watch-chip" key={value}>
                  <button onClick={() => setSymbol(value)}>{value}</button>
                  <button aria-label={`Remove ${value} from watchlist`} onClick={() => toggleWatch(value)}>×</button>
                </span>
              ))}
            </div>
            {markets.length === 0 && !marketsLoading && !error && <p className="empty-state">The backend returned no markets.</p>}
          </section>

          {symbol && timeframe && <LiquidityChart symbol={symbol} timeframe={timeframe} candles={candles} levels={levels} events={events} loading={loading} />}

          <div className="explorer-layout" id="event-explorer">
            <section className="events-section">
              <div className="section-heading">
                <div><h2>Event explorer</h2><p>Events for the selected symbol and timeframe. Displayed timestamps are UTC.</p></div>
                <span className="subtle-tag">{filteredEvents.length} RECORDS</span>
              </div>
              <div className="filters">
                <div className="search-box"><span>⌕</span><input aria-label="Search events" placeholder="Search symbol, event or ID..." value={search} onChange={(event) => setSearch(event.currentTarget.value)} /></div>
                <select aria-label="Filter event type" value={filter} onChange={(event) => setFilter(event.currentTarget.value)}>
                  {eventTypes.map((type) => <option key={type} value={type}>{type === "ALL" ? "All event types" : formatEvent(type)}</option>)}
                </select>
              </div>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>EVENT / MARKET</th><th>TYPE</th><th>UTC TIME</th><th>PRICE</th><th>VOLUME RATIO</th></tr></thead>
                  <tbody>
                    {filteredEvents.map((event) => <tr key={event.event_id} className={selectedEvent?.event_id === event.event_id ? "selected-row" : ""} onClick={() => setSelectedId(event.event_id)}>
                      <td><strong>{event.event_id}</strong><span className="cell-subtitle">{event.symbol}</span></td>
                      <td><span className={`event-badge ${event.event_type.toLowerCase()}`}>{formatEvent(event.event_type)}</span></td>
                      <td>{formatUtcTime(event.timestamp)}</td>
                      <td className="price-cell">{formatPrice(event.price)}</td>
                      <td>{event.volume_ratio.toFixed(2)}×</td>
                    </tr>)}
                    {!loading && filteredEvents.length === 0 && <tr><td colSpan={5} className="empty-state">{events.length === 0 ? "No events for this market and timeframe." : "No events match your filters."}</td></tr>}
                  </tbody>
                </table>
              </div>
            </section>

            <aside className="detail-panel">
              <div className="detail-header"><span className="eyebrow">SELECTED EVENT</span><span className="detail-symbol">◈</span></div>
              {selectedEvent ? <>
                <h3>{selectedEvent.symbol}</h3>
                <p className="detail-id">{selectedEvent.event_id}</p>
                <span className="event-badge">{formatEvent(selectedEvent.event_type)}</span>
                <div className="detail-price"><span>Recorded price</span><strong>{formatPrice(selectedEvent.price)}</strong></div>
                <div className="detail-fields">
                  <DetailField label="TIMEFRAME" value={selectedEvent.timeframe} />
                  <DetailField label="VOLUME RATIO" value={`${selectedEvent.volume_ratio.toFixed(2)}×`} />
                  <DetailField label="MARKET STRUCTURE" value={formatEvent(selectedEvent.structure)} />
                  <DetailField label="TIMESTAMP (UTC)" value={formatUtcTime(selectedEvent.timestamp)} />
                </div>
                <div className="evidence-note"><strong>Evidence record</strong><p>This panel displays supplied event attributes. It does not establish a trade outcome or predict future prices.</p></div>
              </> : <p className="empty-state">Select an event to inspect it.</p>}
            </aside>
          </div>

          <section className="prediction-panel" id="prediction-panel">
            <div className="prediction-icon">◎</div>
            <div className="prediction-copy"><span className="eyebrow">MACHINE LEARNING</span><h2>Prediction engine (later phase)</h2><p>The event dashboard is connected. Predictive inference is not yet configured because we still need labeled historical examples and a validated prediction target.</p></div>
            <span className="model-status">NOT CONNECTED</span>
          </section>
          <footer className="page-footer"><span>HUNTER SIGNAL LAB</span><span>Research tooling · Not financial advice</span></footer>
        </section>
      </main>
    </div>
  );
}

function Metric({ label, value, note, icon }: { label: string; value: string; note: string; icon: string }) {
  return <div className="metric-card"><div className="metric-top"><span>{label}</span><span className="metric-icon">{icon}</span></div><strong className="metric-value">{value}</strong><span className="metric-note">{note}</span></div>;
}

function DetailField({ label, value }: { label: string; value: string }) {
  return <div className="detail-field"><span>{label}</span><strong>{value}</strong></div>;
}

export default App;

// END OF FILE frontend/src/App.tsx