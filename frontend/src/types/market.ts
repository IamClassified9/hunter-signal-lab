
export type Market = {
  symbol: string;
  timeframes: string[];
  exchange: string;
};

export type MarketCandle = {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type LiquidityLevelKind =
  | "SWING_HIGH"
  | "SWING_LOW"
  | "EQUAL_HIGHS"
  | "EQUAL_LOWS";

export type LiquidityLevel = {
  level_id: string;
  symbol: string;
  timeframe: string;
  kind: LiquidityLevelKind;
  price: number;
  formed_at: string;
  confirmed_at: string;
  touches: number;
  swept: boolean;
};

export type LiquidityEvent = {
  event_id: string;
  symbol: string;
  timeframe: string;
  event_type: string;
  price: number;
  volume_ratio: number;
  structure: string;
  timestamp: string;
  level_id?: string;
  level_price?: number;
  level_swept?: number | string;
  detected_at?: string;
};

export type AnalysisResponse = {
  symbol: string;
  timeframe: string;
  exchange: string;
  generated_at: string;
  candles_analysed: number;
  config: Record<string, unknown>;
  levels: LiquidityLevel[];
  events: LiquidityEvent[];
};

export type CandlesResponse = {
  symbol: string;
  timeframe: string;
  exchange: string;
  data_source: string;
  generated_at: string;
  candles: MarketCandle[];
};

export type SummaryResponse = {
  total_events: number;
  symbols: string[];
  event_counts: Record<string, number>;
  data_source: string;
  generated_at: string;
};

export type ApiErrorBody = {
  detail: string;
  code: string;
};
