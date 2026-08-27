import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { LayerOneMutationReceipt, LayerOneRiskStateView } from "../api/types";
import {
  CONFIRM_INITIALIZE,
  CONFIRM_MANUAL_CEILING,
  LOCK_PERSISTENCE_NOTICE,
} from "../lib/layerOneConstants";
import { RiskStateBanner } from "./RiskStateBanner";
import { LayerOneRiskPanel } from "./LayerOneRiskPanel";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const initializeLayerOneRiskState = vi.fn();
const authorizeLayerOneManualCeiling = vi.fn();
const getLayerOneRiskState = vi.fn();

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return {
    ...actual,
    initializeLayerOneRiskState: (body: unknown) => initializeLayerOneRiskState(body),
    authorizeLayerOneManualCeiling: (body: unknown) => authorizeLayerOneManualCeiling(body),
    getLayerOneRiskState: () => getLayerOneRiskState(),
    registerLayerOneDeploymentEvidence: vi.fn(),
    submitLayerOneUnlockRequest: vi.fn(),
    getLayerOneAudit: vi.fn(),
  };
});

const baseFlags = {
  research_only: true as const,
  implementation_only: true as const,
  ready_for_orders: false as const,
  ready_for_trading: false as const,
  does_not_trade: true as const,
};

function uninitialized(): LayerOneRiskStateView {
  return {
    stream_name: "layer-one-primary",
    initialized: false,
    revision: null,
    state_id: null,
    applied_stock_budget: 0,
    effective_stock_budget: 0,
    manual_ceiling: 0,
    manual_ceiling_authorization_id: null,
    risk_lock_active: null,
    risk_lock_triggered_as_of: null,
    red_line_breached: null,
    last_decision_id: null,
    last_decision_target_trading_day: null,
    last_audit_id: null,
    data_snapshot_id: null,
    two_layer_decision_contract_id: null,
    layer_one_index_protocol_id: null,
    initialized_at: null,
    updated_at: null,
    ...baseFlags,
  };
}

function unlockedStale(): LayerOneRiskStateView {
  return {
    ...uninitialized(),
    initialized: true,
    revision: 3,
    state_id: "c".repeat(64),
    applied_stock_budget: 0.3,
    effective_stock_budget: 0.3,
    manual_ceiling: 0.3,
    risk_lock_active: false,
    last_decision_target_trading_day: "2024-06-01",
    data_snapshot_id: "snap-stale",
  };
}

function locked(): LayerOneRiskStateView {
  return {
    ...uninitialized(),
    initialized: true,
    revision: 9,
    state_id: "c".repeat(64),
    applied_stock_budget: 0,
    effective_stock_budget: 0,
    manual_ceiling: 0.3,
    risk_lock_active: true,
    risk_lock_triggered_as_of: "2024-05-10",
  };
}

function unlocked(): LayerOneRiskStateView {
  return {
    ...uninitialized(),
    initialized: true,
    revision: 1,
    state_id: "f".repeat(64),
    risk_lock_active: false,
    applied_stock_budget: 0,
    effective_stock_budget: 0,
    manual_ceiling: 0,
  };
}

function setNativeValue(element: HTMLInputElement, value: string) {
  const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
  descriptor?.set?.call(element, value);
  element.dispatchEvent(new Event("input", { bubbles: true }));
  element.dispatchEvent(new Event("change", { bubbles: true }));
}

function fillInitializeForm(form: HTMLFormElement) {
  act(() => {
    setNativeValue(form.querySelectorAll("input")[0] as HTMLInputElement, "alice");
    setNativeValue(form.querySelectorAll("input")[1] as HTMLInputElement, "boot");
    setNativeValue(form.querySelectorAll("input")[2] as HTMLInputElement, "snap");
    setNativeValue(form.querySelector("#confirm-initialize") as HTMLInputElement, CONFIRM_INITIALIZE);
  });
}

function fillManualCeilingForm(form: HTMLFormElement) {
  act(() => {
    setNativeValue(form.querySelectorAll("input")[0] as HTMLInputElement, "req-refresh-fail");
    setNativeValue(form.querySelectorAll("input")[1] as HTMLInputElement, "alice");
    setNativeValue(form.querySelectorAll("input")[2] as HTMLInputElement, "adjust");
    setNativeValue(form.querySelectorAll("input")[3] as HTMLInputElement, "snap");
    setNativeValue(form.querySelector("#confirm-ceiling") as HTMLInputElement, CONFIRM_MANUAL_CEILING);
  });
}

describe("layer-one mutation refresh semantics", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    initializeLayerOneRiskState.mockReset();
    authorizeLayerOneManualCeiling.mockReset();
    getLayerOneRiskState.mockReset();
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
  });

  it("refreshes risk state after successful mutation", async () => {
    const receipt: LayerOneMutationReceipt = {
      stream_name: "layer-one-primary",
      event_type: "initialize",
      audit_id: "d".repeat(64),
      revision: 1,
      research_only: true,
      implementation_only: true,
      ready_for_orders: false,
      ready_for_trading: false,
      does_not_trade: true,
    };
    initializeLayerOneRiskState.mockResolvedValue(receipt);
    getLayerOneRiskState.mockResolvedValue(unlocked());
    const onRefreshed = vi.fn();
    const onRefreshFailed = vi.fn();
    const onSuccess = vi.fn();
    const onError = vi.fn();

    act(() => {
      root.render(
        <LayerOneRiskPanel
          riskState={uninitialized()}
          riskError={null}
          riskLoading={false}
          mutationError={null}
          mutationBusy={false}
          setMutationBusy={() => undefined}
          onRiskStateRefreshed={onRefreshed}
          onRiskStateRefreshFailed={onRefreshFailed}
          onMutationSuccess={onSuccess}
          onMutationError={onError}
        />,
      );
    });

    const form = container.querySelector('form[aria-label="初始化第一层风险状态"]') as HTMLFormElement;
    fillInitializeForm(form);

    await act(async () => {
      form.requestSubmit();
    });

    expect(initializeLayerOneRiskState).toHaveBeenCalledTimes(1);
    expect(getLayerOneRiskState).toHaveBeenCalledTimes(1);
    expect(onSuccess).toHaveBeenCalledWith(receipt);
    expect(onRefreshed).toHaveBeenCalled();
    expect(onRefreshFailed).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

  it("keeps prior verified state on POST failure", async () => {
    initializeLayerOneRiskState.mockRejectedValue({ detail: "CAS rejected" });
    const onRefreshed = vi.fn();
    const onRefreshFailed = vi.fn();
    const onSuccess = vi.fn();
    let mutationError: string | null = null;

    act(() => {
      root.render(
        <>
          <RiskStateBanner loading={false} error={null} state={locked()} />
          <LayerOneRiskPanel
            riskState={uninitialized()}
            riskError={null}
            riskLoading={false}
            mutationError={mutationError}
            mutationBusy={false}
            setMutationBusy={() => undefined}
            onRiskStateRefreshed={onRefreshed}
            onRiskStateRefreshFailed={onRefreshFailed}
            onMutationSuccess={onSuccess}
            onMutationError={(detail) => {
              mutationError = detail;
            }}
          />
        </>,
      );
    });

    expect(container.querySelector('[role="alert"]')?.textContent).toContain("风险锁定");

    const form = container.querySelector('form[aria-label="初始化第一层风险状态"]') as HTMLFormElement;
    fillInitializeForm(form);

    await act(async () => {
      form.requestSubmit();
    });

    expect(onRefreshed).not.toHaveBeenCalled();
    expect(onRefreshFailed).not.toHaveBeenCalled();
    expect(onSuccess).not.toHaveBeenCalled();
    expect(mutationError).toBe("CAS rejected");
    expect(getLayerOneRiskState).not.toHaveBeenCalled();

    act(() => {
      root.render(
        <>
          <RiskStateBanner loading={false} error={null} state={locked()} />
          <LayerOneRiskPanel
            riskState={uninitialized()}
            riskError={null}
            riskLoading={false}
            mutationError={mutationError}
            mutationBusy={false}
            setMutationBusy={() => undefined}
            onRiskStateRefreshed={onRefreshed}
            onRiskStateRefreshFailed={onRefreshFailed}
            onMutationSuccess={onSuccess}
            onMutationError={() => undefined}
          />
        </>,
      );
    });

    expect(container.textContent).toContain("变更失败（状态未假定已更新）");
    expect(container.textContent).toContain("CAS rejected");
    expect(container.querySelector(".risk-banner-critical")?.textContent).toContain("风险锁定");
    expect(container.textContent).toContain(LOCK_PERSISTENCE_NOTICE);
  });

  it("invalidates stale unlocked state when POST succeeds but refresh fails", async () => {
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
    authorizeLayerOneManualCeiling.mockResolvedValue(receipt);
    getLayerOneRiskState.mockRejectedValue({ detail: "integrity chain broken" });

    let riskError: string | null = null;
    let riskState: LayerOneRiskStateView | null = unlockedStale();
    let mutationError: string | null = null;

    act(() => {
      root.render(
        <>
          <RiskStateBanner loading={false} error={riskError} state={riskState} />
          <LayerOneRiskPanel
            riskState={riskState}
            riskError={riskError}
            riskLoading={false}
            mutationError={mutationError}
            mutationBusy={false}
            setMutationBusy={() => undefined}
            onRiskStateRefreshed={() => undefined}
            onRiskStateRefreshFailed={(detail) => {
              riskState = null;
              riskError = detail;
              mutationError = `刷新风险状态失败：${detail}`;
            }}
            onMutationSuccess={() => {
              mutationError = null;
            }}
            onMutationError={(detail) => {
              mutationError = detail;
            }}
          />
        </>,
      );
    });

    expect(container.textContent).toContain("已初始化且当前未锁定");

    const form = container.querySelector('form[aria-label="人工风险上限授权"]') as HTMLFormElement;
    fillManualCeilingForm(form);

    await act(async () => {
      form.requestSubmit();
    });

    expect(authorizeLayerOneManualCeiling).toHaveBeenCalledTimes(1);
    expect(getLayerOneRiskState).toHaveBeenCalledTimes(1);
    expect(riskError).toBe("integrity chain broken");
    expect(riskState).toBeNull();
    expect(mutationError).toBe("刷新风险状态失败：integrity chain broken");

    act(() => {
      root.render(
        <>
          <RiskStateBanner loading={false} error={riskError} state={riskState} />
          <LayerOneRiskPanel
            riskState={riskState}
            riskError={riskError}
            riskLoading={false}
            mutationError={mutationError}
            mutationBusy={false}
            setMutationBusy={() => undefined}
            onRiskStateRefreshed={() => undefined}
            onRiskStateRefreshFailed={() => undefined}
            onMutationSuccess={() => undefined}
            onMutationError={() => undefined}
          />
        </>,
      );
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
  });
});
