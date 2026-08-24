import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, createBacktest, extractErrorDetail, getHealth, getRanking, getStrategies } from "./client";

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
