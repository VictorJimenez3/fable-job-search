import {describe, expect, it} from "vitest";
import {safeHttpURL} from "./contracts";

describe("safeHttpURL", () => {
  it("allows only credential-free http(s) links", () => {
    expect(safeHttpURL("https://example.com/jobs/1")).toBe("https://example.com/jobs/1");
    expect(safeHttpURL("javascript:alert(1)")).toBe("");
    expect(safeHttpURL("https://user:pass@example.com")).toBe("");
  });
});
