import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import UploadPlugModal, { parsePlugMessage } from "./UploadPlugModal";

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

  it("reports a plug on another contract version instead of timing out", () => {
    expect(parsePlugMessage({ type: "error", contract: "2", code: "PLUG_REJECTED" }, "1")).toEqual({
      type: "error",
      contract: "2",
      code: "PLUG_CONTRACT_MISMATCH",
    });
    expect(parsePlugMessage({ type: "done", contract: "2", thread_id: "t1", handle: HANDLE }, "1")).toBeNull();
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


const PLUG = {
  origin: "http://127.0.0.1:8090",
  contract: "1",
  caps: { max_files: 10, max_file_bytes: 1, max_total_bytes: 1 },
};

function renderModal() {
  const callbacks = { onDone: vi.fn(), onCancel: vi.fn(), onError: vi.fn() };
  const file = new File(["zip"], "study.zip");
  render(<UploadPlugModal plug={PLUG as never} threadId="t1" file={file} {...callbacks} />);
  const frame = screen.getByTitle("Study upload preparation") as HTMLIFrameElement;
  const target = frame.contentWindow as Window;
  const posted = vi.spyOn(target, "postMessage").mockImplementation(() => undefined);
  const send = (data: unknown, origin = PLUG.origin, source: unknown = target) =>
    act(() => {
      window.dispatchEvent(new MessageEvent("message", { data, origin, source: source as Window }));
    });
  return { callbacks, file, frame, posted, send };
}

describe("UploadPlugModal message gate", () => {
  afterEach(() => vi.useRealTimers());

  it("posts the file only to the plug origin after a ready from the frame", () => {
    const { file, posted, send, frame } = renderModal();
    expect(frame.getAttribute("src")).toBe("http://127.0.0.1:8090/embed?thread_id=t1&contract=1");
    send({ type: "ready", contract: "1" });
    expect(posted).toHaveBeenCalledTimes(1);
    const [message, origin] = posted.mock.calls[0] as [Record<string, unknown>, string];
    expect(origin).toBe(PLUG.origin);
    expect(message.type).toBe("file");
    expect(message.file).toBe(file);
    expect(message.thread_id).toBe("t1");
  });

  it("ignores messages from another origin or another window", () => {
    const { callbacks, posted, send } = renderModal();
    send({ type: "ready", contract: "1" }, "http://evil.example");
    send({ type: "ready", contract: "1" }, PLUG.origin, window);
    send({ type: "done", contract: "1", thread_id: "t1", handle: HANDLE }, "http://evil.example");
    expect(posted).not.toHaveBeenCalled();
    expect(callbacks.onDone).not.toHaveBeenCalled();
  });

  it("hands back the handle once", () => {
    const first = renderModal();
    first.send({ type: "done", contract: "1", thread_id: "t1", handle: HANDLE });
    first.send({ type: "done", contract: "1", thread_id: "t1", handle: HANDLE });
    expect(first.callbacks.onDone).toHaveBeenCalledTimes(1);
    expect(first.callbacks.onDone).toHaveBeenCalledWith(HANDLE);
  });

  it("treats a done for another thread as a rejection", () => {
    const { callbacks, send } = renderModal();
    send({ type: "done", contract: "1", thread_id: "other", handle: HANDLE });
    expect(callbacks.onDone).not.toHaveBeenCalled();
    expect(callbacks.onError).toHaveBeenCalledWith("PLUG_REJECTED");
  });

  it("reports unreachable when the frame never says ready", () => {
    vi.useFakeTimers();
    const { callbacks } = renderModal();
    act(() => {
      vi.advanceTimersByTime(20_000);
    });
    expect(callbacks.onError).toHaveBeenCalledWith("PLUG_UNREACHABLE");
  });
});
