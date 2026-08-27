import type {
  BacktestCreated,
  BacktestRecord,
  HealthResponse,
  LayerOneAuditPage,
  LayerOneDeploymentEvidenceRequest,
  LayerOneInitializeRequest,
  LayerOneManualCeilingAuthorizationRequest,
  LayerOneMutationReceipt,
  LayerOneRiskStateView,
  LayerOneUnlockRequestSubmission,
  RankingResponse,
  StrategyInfo,
} from "./types";

export const API_BASE = "/api";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export function extractErrorDetail(payload: unknown, fallback: string): string {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) {
      return detail;
    }
    if (Array.isArray(detail) && detail.length > 0) {
      return JSON.stringify(detail);
    }
  }
  return fallback;
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) {
    return null;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    throw new ApiError(
      response.status,
      extractErrorDetail(payload, `${response.status} ${response.statusText}`),
    );
  }
  return payload as T;
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>("/health");
}

export function getStrategies(): Promise<StrategyInfo[]> {
  return request<StrategyInfo[]>("/strategies");
}

export function getRanking(params: {
  date: string;
  strategy: string;
  top: number;
}): Promise<RankingResponse> {
  const query = new URLSearchParams({
    date: params.date,
    strategy: params.strategy,
    top: String(params.top),
  });
  return request<RankingResponse>(`/ranking?${query.toString()}`);
}

export function createBacktest(body: {
  strategy: string;
  start: string;
  end: string;
}): Promise<BacktestCreated> {
  return request<BacktestCreated>("/backtests", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function getBacktest(id: string): Promise<BacktestRecord> {
  return request<BacktestRecord>(`/backtests/${encodeURIComponent(id)}`);
}

export function getLayerOneRiskState(): Promise<LayerOneRiskStateView> {
  return request<LayerOneRiskStateView>("/layer-one/risk-state");
}

export function initializeLayerOneRiskState(
  body: LayerOneInitializeRequest,
): Promise<LayerOneMutationReceipt> {
  return request<LayerOneMutationReceipt>("/layer-one/risk-state/initialize", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function registerLayerOneDeploymentEvidence(
  body: LayerOneDeploymentEvidenceRequest,
): Promise<LayerOneMutationReceipt> {
  return request<LayerOneMutationReceipt>("/layer-one/deployment-evidence", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function authorizeLayerOneManualCeiling(
  body: LayerOneManualCeilingAuthorizationRequest,
): Promise<LayerOneMutationReceipt> {
  return request<LayerOneMutationReceipt>("/layer-one/manual-ceiling-authorizations", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function submitLayerOneUnlockRequest(
  body: LayerOneUnlockRequestSubmission,
): Promise<LayerOneMutationReceipt> {
  return request<LayerOneMutationReceipt>("/layer-one/unlock-requests", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function getLayerOneAudit(params?: {
  after_sequence?: number;
  page_size?: number;
}): Promise<LayerOneAuditPage> {
  const query = new URLSearchParams({
    after_sequence: String(params?.after_sequence ?? 0),
    page_size: String(params?.page_size ?? 20),
  });
  return request<LayerOneAuditPage>(`/layer-one/audit?${query.toString()}`);
}
