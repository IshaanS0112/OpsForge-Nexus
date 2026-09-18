import { describe, expect, it } from "vitest";
import { parseUtc } from "../api/client";
import { fmtBurn, fmtMs, fmtPct, fmtSeconds } from "./format";

describe("formatters", () => {
  it("formats burn rates by magnitude", () => {
    expect(fmtBurn(0.4231)).toBe("0.42×");
    expect(fmtBurn(14.4)).toBe("14.4×");
    expect(fmtBurn(1000)).toBe("1,000×");
    expect(fmtBurn(null)).toBe("—");
  });

  it("formats latency and percentages", () => {
    expect(fmtMs(4.25)).toBe("4.3 ms");
    expect(fmtMs(812)).toBe("812 ms");
    expect(fmtMs(1500)).toBe("1.50 s");
    expect(fmtPct(0.0123, 2)).toBe("1.23%");
    expect(fmtSeconds(75)).toBe("1m 15s");
  });
});

describe("parseUtc", () => {
  it("reads offset-less backend timestamps as UTC", () => {
    expect(parseUtc("2026-09-18T12:00:00")!.toISOString()).toBe("2026-09-18T12:00:00.000Z");
    expect(parseUtc("2026-09-18T12:00:00Z")!.toISOString()).toBe("2026-09-18T12:00:00.000Z");
    expect(parseUtc("2026-09-18T12:00:00+02:00")!.toISOString()).toBe("2026-09-18T10:00:00.000Z");
    expect(parseUtc(null)).toBeNull();
  });
});
