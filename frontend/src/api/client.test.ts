import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  authorizeLayerOneManualCeiling,
  createBacktest,
  extractErrorDetail,
  getHealth,
  getLayerOneAudit,
  getLayerOneRiskState,
  getRanking,
  getStrategies,
  initializeLayerOneRiskState,
  registerLayerOneDeploymentEvidence,
  submitLayerOneUnlockRequest,
} from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("extractErrorDetail", () => {
  it("keeps FastAPI string detail", () => {
    expect(extractErrorDetail({ detail: "request start is before signal_ready_start" }, "fallback")).toBe(
      "request start is before signal_ready_start",
    );
  });

  it("does not invent a detail when the body is empty", () => {
    expect(extractErrorDetail(null, "503 Service Unavailable")).toBe("503 Service Unavailable");
  });
});

describe("api client", () => {
  it("reads health and strategies from the backend payload", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/health")) {
        return new Response(JSON.stringify({ status: "ok" }), { status: 200 });
      }
      if (url.endsWith("/strategies")) {
        return new Response(JSON.stringify([{ name: "baseline_v1", version: "1.0.0", config_hash: "abc" }]), {
          status: 200,
        });
      }
      throw new Error(`unexpected url ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getHealth()).resolves.toEqual({ status: "ok" });
    await expect(getStrategies()).resolves.toEqual([
      { name: "baseline_v1", version: "1.0.0", config_hash: "abc" },
    ]);
  });

  it("surfaces ranking preflight errors without fabricating items", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        return new Response(JSON.stringify({ detail: "cannot determine signal_ready_start" }), { status: 400 });
      }),
    );

    await expect(getRanking({ date: "2024-01-02", strategy: "baseline_v1", top: 5 })).rejects.toMatchObject({
      name: "ApiError",
      status: 400,
      detail: "cannot determine signal_ready_start",
    } satisfies Partial<ApiError>);
  });

  it("surfaces backtest errors without fabricating metrics", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        return new Response(JSON.stringify({ detail: "missing manifest.json" }), { status: 400 });
      }),
    );

    await expect(
      createBacktest({ strategy: "baseline_v1", start: "2024-01-02", end: "2024-01-31" }),
    ).rejects.toBeInstanceOf(ApiError);
    await expect(
      createBacktest({ strategy: "baseline_v1", start: "2024-01-02", end: "2024-01-31" }),
    ).rejects.toMatchObject({ detail: "missing manifest.json" });
  });
});

describe("layer-one api client", () => {
  it("GETs risk-state and audit on expected paths", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/layer-one/risk-state")) {
        return new Response(
          JSON.stringify({
            stream_name: "layer-one-primary",
            initialized: false,
            applied_stock_budget: 0,
            effective_stock_budget: 0,
            manual_ceiling: 0,
            research_only: true,
            implementation_only: true,
            ready_for_orders: false,
            ready_for_trading: false,
            does_not_trade: true,
          }),
          { status: 200 },
        );
      }
      if (url.includes("/layer-one/audit?")) {
        expect(url).toContain("page_size=20");
        expect(url).toContain("after_sequence=0");
        return new Response(
          JSON.stringify({
            stream_name: "layer-one-primary",
            items: [],
            next_after_sequence: null,
            page_size: 20,
            research_only: true,
            ready_for_orders: false,
            ready_for_trading: false,
            does_not_trade: true,
          }),
          { status: 200 },
        );
      }
      throw new Error(`unexpected url ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await getLayerOneRiskState();
    await getLayerOneAudit({ after_sequence: 0, page_size: 20 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("POSTs initialize / ceiling / unlock / evidence with correct methods and preserves detail", async () => {
    const calls: Array<{ url: string; method: string }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, method: init?.method ?? "GET" });
        if (url.endsWith("/layer-one/risk-state/initialize")) {
          return new Response(JSON.stringify({ detail: "initialization requires user_confirmed=true" }), {
            status: 400,
          });
        }
        return new Response(
          JSON.stringify({
            stream_name: "layer-one-primary",
            event_type: "manual_ceiling_authorization",
            audit_id: "a".repeat(64),
            revision: 2,
            research_only: true,
            implementation_only: true,
            ready_for_orders: false,
            ready_for_trading: false,
            does_not_trade: true,
          }),
          { status: 200 },
        );
      }),
    );

    await expect(
      initializeLayerOneRiskState({
        operator: "op",
        reason: "r",
        initialized_at: "2024-01-02T00:00:00.000Z",
        user_confirmed: false,
        two_layer_decision_contract_id: "a".repeat(64),
        layer_one_index_protocol_id: "b".repeat(64),
        data_snapshot_id: "snap",
      }),
    ).rejects.toMatchObject({ detail: "initialization requires user_confirmed=true" });

    await authorizeLayerOneManualCeiling({
      request_id: "req-1",
      ceiling: 0,
      authorized_at: "2024-01-02T00:00:00.000Z",
      operator: "op",
      reason: "r",
      user_confirmed: true,
      two_layer_decision_contract_id: "a".repeat(64),
      layer_one_index_protocol_id: "b".repeat(64),
      data_snapshot_id: "snap",
      auto_upgrade: false,
    });
    await submitLayerOneUnlockRequest({
      request: {
        request_id: "u1",
        operator: "op",
        reason: "r",
        requested_at: "2024-01-02T00:00:00.000Z",
        user_confirmed: true,
      },
      two_layer_decision_contract_id: "a".repeat(64),
      layer_one_index_protocol_id: "b".repeat(64),
      data_snapshot_id: "snap",
    });
    await registerLayerOneDeploymentEvidence({
      evidence_type: "historical_validation_pass",
      observed_from: "2024-01-01",
      observed_through: "2024-06-01",
      recorded_at: "2024-01-02T00:00:00.000Z",
      operator: "op",
      summary: "ok",
      user_confirmed: true,
      two_layer_decision_contract_id: "a".repeat(64),
      layer_one_index_protocol_id: "b".repeat(64),
      data_snapshot_id: "snap",
      historical_validation_pass: true,
      no_severe_anomaly: null,
    });

    expect(calls.map((c) => [c.method, c.url.replace(/^.*\/api/, "/api")])).toEqual([
      ["POST", "/api/layer-one/risk-state/initialize"],
      ["POST", "/api/layer-one/manual-ceiling-authorizations"],
      ["POST", "/api/layer-one/unlock-requests"],
      ["POST", "/api/layer-one/deployment-evidence"],
    ]);
  });

  it("does not expose broker or order client helpers", async () => {
    const client = await import("./client");
    const names = Object.keys(client);
    expect(names.some((n) => /broker|order|placeOrder|trade/i.test(n))).toBe(false);
  });
});
