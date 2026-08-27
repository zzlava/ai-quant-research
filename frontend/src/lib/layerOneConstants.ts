/** Bound upstream contract digests (must match backend disk binding). */
export const BOUND_TWO_LAYER_DECISION_CONTRACT_ID =
  "27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6";

export const BOUND_LAYER_ONE_INDEX_PROTOCOL_ID =
  "b7aa9de1539cdd791aee5b74ca8ec3f269b6ed809a070caa917686742c4b1b2f";

export const CONFIRM_INITIALIZE = "初始化为0%且不代表可交易";
export const CONFIRM_MANUAL_CEILING = "我确认仅人工调整风险上限";
export const CONFIRM_UNLOCK_REQUEST = "我确认仅提交解锁申请且不会立即解锁";
export const CONFIRM_DEPLOYMENT_EVIDENCE = "我确认这是人工登记证据且仍需独立审查";

export const LOCK_PERSISTENCE_NOTICE =
  "重启不会解除；申请解锁本身不会解除；仍需20个交易日冷静期、指数非负趋势、60日波动率<27%及密封决策通过";

export function nowIso(): string {
  return new Date().toISOString();
}

export function abbreviateId(id: string | null | undefined, keep = 8): string {
  if (!id) return "—";
  if (id.length <= keep) return id;
  return `${id.slice(0, keep)}…`;
}
