import { describe, expect, it } from "vitest";
import { parsePlugMessage } from "./UploadPlugModal";

const HANDLE = "a".repeat(32);

describe("parsePlugMessage", () => {
  it("accepts the four message types for the right contract", () => {
    expect(parsePlugMessage({ type: "ready", contract: "1" }, "1")).toEqual({
      type: "ready",
      contract: "1",
    });
    expect(
      parsePlugMessage(
        { type: "done", contract: "1", thread_id: "t1", handle: HANDLE },
        "1",
      ),
    ).toEqual({ type: "done", contract: "1", thread_id: "t1", handle: HANDLE });
    expect(parsePlugMessage({ type: "cancel", contract: "1" }, "1")?.type).toBe(
      "cancel",
    );
    expect(
      parsePlugMessage({ type: "error", contract: "1", code: "PHI_BLOCKED" }, "1"),
    ).toEqual({ type: "error", contract: "1", code: "PHI_BLOCKED" });
  });

  it("ignores wrong contracts, unknown types and non-objects", () => {
    expect(parsePlugMessage({ type: "ready", contract: "2" }, "1")).toBeNull();
    expect(parsePlugMessage({ type: "file", contract: "1" }, "1")).toBeNull();
    expect(parsePlugMessage("ready", "1")).toBeNull();
    expect(parsePlugMessage(null, "1")).toBeNull();
  });

  it("rejects done without a valid handle or thread", () => {
    expect(
      parsePlugMessage({ type: "done", contract: "1", thread_id: "t1", handle: "../x" }, "1"),
    ).toBeNull();
    expect(
      parsePlugMessage({ type: "done", contract: "1", handle: HANDLE }, "1"),
    ).toBeNull();
  });

  it("never passes free text through an error code", () => {
    expect(
      parsePlugMessage(
        { type: "error", contract: "1", code: "Jane Doe, born 1950" },
        "1",
      ),
    ).toEqual({ type: "error", contract: "1", code: "PLUG_REJECTED" });
  });
});
