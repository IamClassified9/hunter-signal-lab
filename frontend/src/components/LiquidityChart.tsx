import { useEffect, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import type {
  LiquidityEvent,
  LiquidityLevel,
  MarketCandle,
} from "../types/market";
import { isoTimestampToUtcSeconds } from "../services/api";

type Props = {
  symbol: string;
  timeframe: string;
  candles: MarketCandle[];
  levels: LiquidityLevel[];
  events: LiquidityEvent[];
  loading: boolean;
};

type ChartCandle = {
  time: UTCTimestamp;
  open: number;
  high: number;
  low: number;
  close: number;
};

function shortEventLabel(eventType: string): string {
  const value = eventType.toUpperCase();
  if (value.includes("SWEEP")) return "SWEEP";
  if (value.includes("GRAB")) return "GRAB";
  if (value.includes("FAILED") || value.includes("FAIL")) return "FAIL";
  if (value.includes("BREAKOUT") || value === "BRK") return "BRK";
  return value.length > 5 ? value.slice(0, 5) : value;
}

function markerAppearance(
  event: LiquidityEvent,
  levels: LiquidityLevel[],
): {
  shape: "arrowUp" | "arrowDown" | "circle" | "square";
  position: "aboveBar" | "belowBar";
  color: string;
} {
  const type = event.event_type.toUpperCase();
  const structure = event.structure.toUpperCase();
  const level = event.level_id
    ? levels.find((candidate) => candidate.level_id === event.level_id)
    : undefined;
  const levelKind = level?.kind ?? "";
  const highSide =
    levelKind === "SWING_HIGH" ||
    levelKind === "EQUAL_HIGHS" ||
    /HIGH|RESISTANCE|UPPER/.test(structure);
  const lowSide =
    levelKind === "SWING_LOW" ||
    levelKind === "EQUAL_LOWS" ||
    /LOW|SUPPORT|LOWER/.test(structure);

  if (type === "RESISTANCE_BREAK") {
    return { shape: "arrowUp", position: "aboveBar", color: "#91c9ff" };
  }
  if (type === "SUPPORT_BREAK") {
    return { shape: "arrowDown", position: "belowBar", color: "#91c9ff" };
  }
  if (type.includes("SWEEP") || type.includes("GRAB")) {
    if (highSide) {
      return {
        shape: "arrowDown",
        position: "aboveBar",
        color: type.includes("SWEEP") ? "#e8b86b" : "#d8a0ff",
      };
    }
    if (lowSide) {
      return {
        shape: "arrowUp",
        position: "belowBar",
        color: type.includes("SWEEP") ? "#e8b86b" : "#d8a0ff",
      };
    }
    // When neither structure nor linked level identifies a side, preserve a
    // visible neutral marker rather than guessing high-side or low-side.
    return { shape: "circle", position: "belowBar", color: "#d8a0ff" };
  }
  if (type.includes("FAILED") || type.includes("FAIL")) {
    return { shape: "square", position: "aboveBar", color: "#ff8078" };
  }
  return { shape: "arrowUp", position: "belowBar", color: "#91c9ff" };
}

export default function LiquidityChart({
  symbol,
  timeframe,
  candles,
  levels,
  events,
  loading,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [showSwept, setShowSwept] = useState(false);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    let chart: IChartApi | null = null;
    let candleSeries: ISeriesApi<"Candlestick"> | null = null;
    let markerPlugin: ReturnType<typeof createSeriesMarkers<Time>> | null = null;
    const priceLines: Array<ReturnType<ISeriesApi<"Candlestick">["createPriceLine"]>> = [];

    chart = createChart(container, {
      width: Math.max(container.clientWidth, 320),
      height: 360,
      layout: {
        background: { type: ColorType.Solid, color: "#111612" },
        textColor: "#aab5ac",
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: "#202821" },
        horzLines: { color: "#202821" },
      },
      rightPriceScale: {
        borderColor: "#303a31",
      },
      timeScale: {
        borderColor: "#303a31",
        timeVisible: true,
        secondsVisible: false,
      },
      crosshair: {
        vertLine: { color: "#637567", labelBackgroundColor: "#294a30" },
        horzLine: { color: "#637567", labelBackgroundColor: "#294a30" },
      },
    });

    candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#a4e5a9",
      downColor: "#ed827b",
      borderUpColor: "#a4e5a9",
      borderDownColor: "#ed827b",
      wickUpColor: "#a4e5a9",
      wickDownColor: "#ed827b",
    });

    const loadedCandles: ChartCandle[] = candles
      .map((candle) => ({
        time: isoTimestampToUtcSeconds(candle.timestamp) as UTCTimestamp,
        open: candle.open,
        high: candle.high,
        low: candle.low,
        close: candle.close,
      }))
      .sort((left, right) => Number(left.time) - Number(right.time));

    candleSeries.setData(loadedCandles);

    const loadedTimes = new Set(loadedCandles.map((candle) => Number(candle.time)));
    const markers = events
      .map((event) => {
        const time = isoTimestampToUtcSeconds(event.timestamp) as UTCTimestamp;
        if (!loadedTimes.has(Number(time))) return null;
        const appearance = markerAppearance(event, levels);
        return {
          time,
          position: appearance.position,
          color: appearance.color,
          shape: appearance.shape,
          text: shortEventLabel(event.event_type),
        };
      })
      .filter((marker) => marker !== null)
      .sort((left, right) => Number(left.time) - Number(right.time));

    markerPlugin = createSeriesMarkers(candleSeries, markers);

    for (const level of levels) {
      if (level.swept && !showSwept) continue;
      const cluster = level.kind === "EQUAL_HIGHS" || level.kind === "EQUAL_LOWS";
      priceLines.push(
        candleSeries.createPriceLine({
          price: level.price,
          color: level.swept ? "#66716a" : cluster ? "#d8a0ff" : "#a4e5a9",
          lineWidth: 1,
          lineStyle: cluster ? LineStyle.Dashed : LineStyle.Solid,
          axisLabelVisible: true,
          title: `${level.kind.replaceAll("_", " ")}${cluster ? ` · ${level.touches} touches` : ""}${level.swept ? " · SWEPT" : ""}`,
        }),
      );
    }

    chart.timeScale().fitContent();

    const resizeObserver = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry || !chart) return;
      chart.applyOptions({
        width: Math.max(Math.floor(entry.contentRect.width), 320),
      });
    });
    resizeObserver.observe(container);

    return () => {
      resizeObserver.disconnect();
      if (markerPlugin) markerPlugin.setMarkers([]);
      if (candleSeries) {
        for (const priceLine of priceLines) {
          candleSeries.removePriceLine(priceLine);
        }
      }
      if (chart) {
        chart.remove();
        chart = null;
      }
    };
  }, [candles, events, levels, showSwept]);

  return (
    <section className="chart-panel">
      <div className="section-heading chart-heading">
        <div>
          <h2>Liquidity chart</h2>
          <p>{symbol} · {timeframe} · All displayed times are UTC.</p>
        </div>
        <label className="swept-toggle">
          <input
            type="checkbox"
            checked={showSwept}
            onChange={(event) => setShowSwept(event.currentTarget.checked)}
          />
          Show swept levels
        </label>
      </div>
      {loading && <div className="chart-overlay">Loading chart data…</div>}
      {!loading && candles.length === 0 ? (
        <div className="empty-state chart-empty">No candles are available for this market and timeframe.</div>
      ) : (
        <div ref={containerRef} className="liquidity-chart" aria-label={`UTC candlestick chart for ${symbol} ${timeframe}`} />
      )}
      <div className="chart-legend">
        <span><i className="legend-line swing-line" /> Unswept swing level</span>
        <span><i className="legend-line cluster-line" /> Equal-high/low cluster (dashed)</span>
        <span><i className="legend-line swept-line" /> Swept level (dimmed)</span>
        <span>Markers are shown only when their event timestamp matches a loaded candle open.</span>
      </div>
    </section>
  );
}

// END OF FILE frontend/src/components/LiquidityChart.tsx
