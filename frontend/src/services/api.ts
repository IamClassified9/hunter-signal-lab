import type {
  AnalysisResponse,
  ApiErrorBody,
  CandlesResponse,
  LiquidityEvent,
  Market,
  SummaryResponse,
} from "../types/market";

const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000"
).replace(/\/$/, "");

export class ApiRequestError extends Error {
  readonly code: string | undefined;
  readonly status: number | undefined;

  constructor(message: string, status?: number, code?: string) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
  }
}

async function requestJson<T>(
  path: string,
  signal?: AbortSignal,
): Promise<T> {
  let response: Response;

  try {
    response = await fetch(`${API_BASE_URL}${path}`, { signal });
  } catch (error: unknown) {
    if (error instanceof Error && error.name === "AbortError") {
      throw error;
    }
    throw new ApiRequestError(
      error instanceof Error
        ? error.message
        : "Could not connect to the Hunter API.",
    );
  }

  if (!response.ok) {
    let detail = `Request failed with HTTP ${response.status}.`;
    let code: string | undefined;

    try {
      const body: ApiErrorBody = await response.json() as ApiErrorBody;
      if (typeof body.detail === "string" && body.detail.length > 0) {
        detail = body.detail;
      }
      if (typeof body.code === "string") {
        code = body.code;
      }
    } catch {
      // Keep the HTTP fallback if the response body is not JSON.
    }

    throw new ApiRequestError(detail, response.status, code);
  }

  return response.json() as Promise<T>;
}

function queryString(values: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== "") {
      params.set(key, String(value));
    }
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

/** The single timestamp conversion helper: ISO timestamp -> UTC epoch seconds. */
export function isoTimestampToUtcSeconds(value: string): number {
  const milliseconds = Date.parse(value);
  if (!Number.isFinite(milliseconds)) {
    throw new Error(`Invalid ISO timestamp: ${value}`);
  }
  return Math.floor(milliseconds / 1000);
}

export function getMarkets(signal?: AbortSignal): Promise<Market[]> {
  return requestJson<Market[]>("/api/markets", signal);
}

export function getAnalysis(
  symbol: string,
  timeframe: string,
  limit = 300,
  signal?: AbortSignal,
): Promise<AnalysisResponse> {
  return requestJson<AnalysisResponse>(
    `/api/analysis${queryString({ symbol, timeframe, limit })}`,
    signal,
  );
}

export function getCandles(
  symbol: string,
  timeframe: string,
  limit = 300,
  signal?: AbortSignal,
): Promise<CandlesResponse> {
  return requestJson<CandlesResponse>(
    `/api/candles${queryString({ symbol, timeframe, limit })}`,
    signal,
  );
}

export type MarketSearchResult = { symbol: string; base: string };

export function searchMarkets(
  query: string,
  limit = 10,
  signal?: AbortSignal,
): Promise<MarketSearchResult[]> {
  return requestJson<MarketSearchResult[]>(
    `/api/markets/search${queryString({ q: query, limit })}`,
    signal,
  );
}

export function getEvents(
  limit = 500,
  signal?: AbortSignal,
  symbols?: string[],
): Promise<LiquidityEvent[]> {
  return requestJson<LiquidityEvent[]>(
    `/api/events${queryString({ limit, symbols: symbols?.join(",") })}`,
    signal,
  );
}

export function getSummary(signal?: AbortSignal): Promise<SummaryResponse> {
  return requestJson<SummaryResponse>("/api/summary", signal);
}

// END OF FILE frontend/src/services/api.ts

export type BriefEvent = {
  event_id: string | null;
  evidence: {
    event_id?: string;
    symbol?: string;
    timeframe?: string;
    event_type?: string;
    price?: number;
    volume_ratio?: number;
    structure?: string;
    timestamp?: string;
    level_id?: string | null;
    level_price?: number | null;
    detected_at?: string | null;
  };
  sentences: [string, string] | string[];
  snapshot?: string;
  snapshot_alt?: string;
};

export type BriefResponse = {
  generated_at: string;
  model: string;
  source: "gemma" | "template";
  temperature: number | null;
  fallback_reason?: string | null;
  summary: [string, string] | string[];
  events: BriefEvent[];
  disclaimer: string;
};

export function getBrief(symbols: string[], signal?: AbortSignal): Promise<BriefResponse> {
  return requestJson<BriefResponse>(`/brief${queryString({ symbols: symbols.join(",") })}`, signal);
}
