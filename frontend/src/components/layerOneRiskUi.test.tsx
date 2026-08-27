import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { LayerOneRiskStateView } from "../api/types";
import {
  CONFIRM_INITIALIZE,
  CONFIRM_MANUAL_CEILING,
  CONFIRM_UNLOCK_REQUEST,
  LOCK_PERSISTENCE_NOTICE,
} from "../lib/layerOneConstants";
import { LayerOneRiskPanel } from "./LayerOneRiskPanel";
import { RiskStateBanner } from "./RiskStateBanner";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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

function unlockedResearch(): LayerOneRiskStateView {
  return {
    ...uninitialized(),
    initialized: true,
    revision: 3,
    state_id: "c".repeat(64),
    applied_stock_budget: 0.3,
    effective_stock_budget: 0.3,
    manual_ceiling: 0.3,
    risk_lock_active: false,
    risk_lock_triggered_as_of: null,
    last_decision_target_trading_day: "2024-06-01",
    data_snapshot_id: "snap-1",
  };
}

function locked(): LayerOneRiskStateView {
  return {
    ...unlockedResearch(),
    applied_stock_budget: 0,
    effective_stock_budget: 0,
    risk_lock_active: true,
    risk_lock_triggered_as_of: "2024-05-10",
    revision: 9,
  };
}

function setNativeValue(element: HTMLInputElement, value: string) {
  const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
  descriptor?.set?.call(element, value);
  element.dispatchEvent(new Event("input", { bubbles: true }));
  element.dispatchEvent(new Event("change", { bubbles: true }));
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
});

describe("RiskStateBanner", () => {
  it("shows critical API/integrity fail-closed alert", () => {
    act(() => {
      root.render(<RiskStateBanner loading={false} error="chain broken" state={null} />);
    });
    const alert = container.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain("风险状态无法核验");
    expect(alert?.textContent).toContain("有效股票预算 0%");
    expect(alert?.textContent).toContain("chain broken");
    expect(container.textContent).not.toMatch(/已可交易|可以下单/);
  });

  it("shows uninitialized fail-closed alert", () => {
    act(() => {
      root.render(<RiskStateBanner loading={false} error={null} state={uninitialized()} />);
    });
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("风险状态未初始化");
    expect(container.textContent).toContain("不能视为已解锁");
  });

  it("shows active lock banner with persistence notice", () => {
    act(() => {
      root.render(<RiskStateBanner loading={false} error={null} state={locked()} />);
    });
    const alert = container.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain("风险锁定");
    expect(alert?.textContent).toContain("2024-05-10");
    expect(alert?.textContent).toContain(LOCK_PERSISTENCE_NOTICE);
  });

  it("shows research-only unlocked status without tradable success claim", () => {
    act(() => {
      root.render(<RiskStateBanner loading={false} error={null} state={unlockedResearch()} />);
    });
    const status = container.querySelector('[role="status"]');
    expect(status?.textContent).toContain("仅研究");
    expect(status?.textContent).toContain("ready_for_trading=false");
    expect(container.textContent).not.toMatch(/已可交易|可以交易|safe to trade/i);
  });
});

describe("LayerOneRiskPanel gates", () => {
  it("disables initialize until exact phrase and nonblank fields", () => {
    act(() => {
      root.render(
        <LayerOneRiskPanel
          riskState={uninitialized()}
          riskError={null}
          riskLoading={false}
          mutationError={null}
          mutationBusy={false}
          setMutationBusy={() => undefined}
          onRiskStateRefreshed={() => undefined}
          onRiskStateRefreshFailed={() => undefined}
          onMutationSuccess={() => undefined}
          onMutationError={() => undefined}
        />,
      );
    });
    const form = container.querySelector('form[aria-label="初始化第一层风险状态"]') as HTMLFormElement;
    const button = form.querySelector('button[type="submit"]') as HTMLButtonElement;
    expect(button.disabled).toBe(true);

    act(() => {
      setNativeValue(form.querySelectorAll("input")[0] as HTMLInputElement, "alice");
      setNativeValue(form.querySelectorAll("input")[1] as HTMLInputElement, "boot");
      setNativeValue(form.querySelectorAll("input")[2] as HTMLInputElement, "snap");
      setNativeValue(form.querySelector("#confirm-initialize") as HTMLInputElement, "wrong");
    });
    expect(button.disabled).toBe(true);

    act(() => {
      setNativeValue(form.querySelector("#confirm-initialize") as HTMLInputElement, CONFIRM_INITIALIZE);
    });
    expect(button.disabled).toBe(false);
  });

  it("shows unlock form and warning only during active lock", () => {
    act(() => {
      root.render(
        <LayerOneRiskPanel
          riskState={unlockedResearch()}
          riskError={null}
          riskLoading={false}
          mutationError={null}
          mutationBusy={false}
          setMutationBusy={() => undefined}
          onRiskStateRefreshed={() => undefined}
          onRiskStateRefreshFailed={() => undefined}
          onMutationSuccess={() => undefined}
          onMutationError={() => undefined}
        />,
      );
    });
    expect(container.querySelector('form[aria-label="提交解锁申请"]')).toBeNull();
    expect(container.querySelector(".unlock-warning")).toBeNull();

    act(() => {
      root.render(
        <LayerOneRiskPanel
          riskState={locked()}
          riskError={null}
          riskLoading={false}
          mutationError={null}
          mutationBusy={false}
          setMutationBusy={() => undefined}
          onRiskStateRefreshed={() => undefined}
          onRiskStateRefreshFailed={() => undefined}
          onMutationSuccess={() => undefined}
          onMutationError={() => undefined}
        />,
      );
    });
    const unlock = container.querySelector('form[aria-label="提交解锁申请"]') as HTMLFormElement;
    expect(unlock).not.toBeNull();
    expect(container.querySelector(".unlock-warning")?.textContent).toContain("申请本身不会解除锁定");
    expect((unlock.querySelector('button[type="submit"]') as HTMLButtonElement).disabled).toBe(true);
    expect(CONFIRM_UNLOCK_REQUEST).toContain("不会立即解锁");
  });

  it("keeps ceiling button disabled until exact confirmation phrase", () => {
    act(() => {
      root.render(
        <LayerOneRiskPanel
          riskState={unlockedResearch()}
          riskError={null}
          riskLoading={false}
          mutationError={null}
          mutationBusy={false}
          setMutationBusy={() => undefined}
          onRiskStateRefreshed={() => undefined}
          onRiskStateRefreshFailed={() => undefined}
          onMutationSuccess={() => undefined}
          onMutationError={() => undefined}
        />,
      );
    });
    const form = container.querySelector('form[aria-label="人工风险上限授权"]') as HTMLFormElement;
    const button = form.querySelector('button[type="submit"]') as HTMLButtonElement;
    expect(button.disabled).toBe(true);

    act(() => {
      setNativeValue(form.querySelectorAll("input")[0] as HTMLInputElement, "req-1");
      setNativeValue(form.querySelectorAll("input")[1] as HTMLInputElement, "op");
      setNativeValue(form.querySelectorAll("input")[2] as HTMLInputElement, "reason");
      setNativeValue(form.querySelectorAll("input")[3] as HTMLInputElement, "snap");
      setNativeValue(form.querySelector("#confirm-ceiling") as HTMLInputElement, CONFIRM_MANUAL_CEILING);
    });
    expect(button.disabled).toBe(false);
  });

  it("hides mutation forms including deployment evidence when risk state is unverifiable", () => {
    act(() => {
      root.render(
        <LayerOneRiskPanel
          riskState={null}
          riskError="integrity chain broken"
          riskLoading={false}
          mutationError="刷新风险状态失败：integrity chain broken"
          mutationBusy={false}
          setMutationBusy={() => undefined}
          onRiskStateRefreshed={() => undefined}
          onRiskStateRefreshFailed={() => undefined}
          onMutationSuccess={() => undefined}
          onMutationError={() => undefined}
        />,
      );
    });
    expect(container.querySelector('form[aria-label="人工风险上限授权"]')).toBeNull();
    expect(container.querySelector('form[aria-label="登记部署证据"]')).toBeNull();
    expect(container.querySelector('button[type="button"]')?.textContent).toContain("加载审计");
  });
});
