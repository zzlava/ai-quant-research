import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import { errorMessage, formatNumber, formatPct } from "./format";

describe("format helpers", () => {
  it("does not invent numbers for missing metrics", () => {
    expect(formatNumber(null)).toBe("—");
    expect(formatPct(undefined)).toBe("—");
    expect(formatPct(0.1234)).toBe("12.34%");
  });

  it("shows backend ApiError detail as-is", () => {
    expect(errorMessage(new ApiError(400, "refusing to score a partial universe"))).toBe(
      "refusing to score a partial universe",
    );
  });
});
