import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { LayerOneMutationReceipt, LayerOneRiskStateView } from "./api/types";
import { CONFIRM_MANUAL_CEILING } from "./lib/layerOneConstants";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const unlockedStale: LayerOneRiskStateView = {
  stream_name: "layer-one-primary",
  initialized: true,
  revision: 3,
  state_id: "c".repeat(64),
  applied_stock_budget: 0.3,
  effective_stock_budget: 0.3,
  manual_ceiling: 0.3,
  manual_ceiling_authorization_id: null,
  risk_lock_active: false,
  risk_lock_triggered_as_of: null,
  red_line_breached: false,
  last_decision_id: null,
  last_decision_target_trading_day: "2024-06-01",
  last_audit_id: null,
  data_snapshot_id: "snap-stale",
  two_layer_decision_contract_id: null,
  layer_one_index_protocol_id: null,
  initialized_at: null,
  updated_at: null,
  research_only: true,
  implementation_only: true,
  ready_for_orders: false,
  ready_for_trading: false,
  does_not_trade: true,
};

const getLayerOneRiskState = vi.fn();
const getHealth = vi.fn(async () => ({ status: "ok" }));
const getStrategies = vi.fn(async () => [
  { name: "baseline_v1", version: "1.0.0", config_hash: "abc" },
]);
const authorizeLayerOneManualCeiling = vi.fn();

vi.mock("./api/client", async () => {
  const actual = await vi.importActual<typeof import("./api/client")>("./api/client");
  return {
    ...actual,
    getHealth: () => getHealth(),
    getStrategies: () => getStrategies(),
    getLayerOneRiskState: () => getLayerOneRiskState(),
    authorizeLayerOneManualCeiling: (body: unknown) => authorizeLayerOneManualCeiling(body),
    initializeLayerOneRiskState: vi.fn(),
    registerLayerOneDeploymentEvidence: vi.fn(),
    submitLayerOneUnlockRequest: vi.fn(),
    getRanking: vi.fn(),
    createBacktest: vi.fn(),
    getBacktest: vi.fn(),
  };
});

import { App } from "./App";
import * as client from "./api/client";

function setNativeValue(element: HTMLInputElement, value: string) {
  const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
  descriptor?.set?.call(element, value);
  element.dispatchEvent(new Event("input", { bubbles: true }));
  element.dispatchEvent(new Event("change", { bubbles: true }));
}

describe("App risk-state load", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    getLayerOneRiskState.mockReset();
    getHealth.mockClear();
    getStrategies.mockClear();
    authorizeLayerOneManualCeiling.mockReset();
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
  });

  it("fetches layer-one risk state immediately on load", async () => {
    getLayerOneRiskState.mockResolvedValueOnce({
      ...unlockedStale,
      initialized: false,
      revision: null,
      state_id: null,
      applied_stock_budget: 0,
      effective_stock_budget: 0,
      manual_ceiling: 0,
    });

    await act(async () => {
      root.render(<App />);
    });

    expect(getLayerOneRiskState).toHaveBeenCalled();
    expect(container.textContent).toContain("风险状态未初始化");
    expect(container.textContent).toContain("第一层风险控制（研究/人工操作）");
    expect(container.textContent).not.toMatch(/已可交易|可以下单/);
    expect(Object.keys(client).some((name) => /broker|placeOrder|submitOrder/i.test(name))).toBe(
      false,
    );
  });

  it("fail-closes banner when mutation POST succeeds but risk-state refresh fails", async () => {
    const receipt: LayerOneMutationReceipt = {
      stream_name: "layer-one-primary",
      event_type: "manual_ceiling_authorization",
      audit_id: "d".repeat(64),
      revision: 4,
      research_only: true,
      implementation_only: true,
      ready_for_orders: false,
      ready_for_trading: false,
      does_not_trade: true,
    };

    getLayerOneRiskState.mockResolvedValueOnce(unlockedStale);
    authorizeLayerOneManualCeiling.mockResolvedValueOnce(receipt);
    getLayerOneRiskState.mockRejectedValueOnce({ detail: "integrity chain broken" });

    await act(async () => {
      root.render(<App />);
    });

    expect(container.textContent).toContain("已初始化且当前未锁定");

    const form = container.querySelector('form[aria-label="人工风险上限授权"]') as HTMLFormElement;
    act(() => {
      setNativeValue(form.querySelectorAll("input")[0] as HTMLInputElement, "req-app-refresh-fail");
      setNativeValue(form.querySelectorAll("input")[1] as HTMLInputElement, "alice");
      setNativeValue(form.querySelectorAll("input")[2] as HTMLInputElement, "adjust");
      setNativeValue(form.querySelectorAll("input")[3] as HTMLInputElement, "snap");
      setNativeValue(form.querySelector("#confirm-ceiling") as HTMLInputElement, CONFIRM_MANUAL_CEILING);
    });

    await act(async () => {
      form.requestSubmit();
    });

    const banner = container.querySelector(".risk-banner-critical[role='alert']");
    expect(banner?.textContent).toContain("风险状态无法核验");
    expect(banner?.textContent).toContain("失败关闭");
    expect(banner?.textContent).toContain("有效股票预算 0%");
    expect(banner?.textContent).toContain("integrity chain broken");
    expect(container.textContent).not.toContain("已初始化且当前未锁定");
    expect(container.textContent).toContain("变更后无法重新核验风险状态（当前状态已作废）");
    expect(container.textContent).toContain("刷新风险状态失败：integrity chain broken");
    expect(container.querySelector('form[aria-label="登记部署证据"]')).toBeNull();
    expect(getLayerOneRiskState).toHaveBeenCalledTimes(2);
  });
});
