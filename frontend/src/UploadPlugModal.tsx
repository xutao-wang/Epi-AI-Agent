import { useEffect, useRef } from "react";
import type { UploadPlugInfo } from "./types";

// Messages from the plug page. Anything else is ignored.
type PlugMessage =
  | { type: "ready"; contract: string }
  | { type: "done"; contract: string; thread_id: string; handle: string }
  | { type: "cancel"; contract: string }
  | { type: "error"; contract: string; code: string };

const READY_TIMEOUT_MS = 20_000;
const HANDLE_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;
const CODE_PATTERN = /^[A-Z_]{1,64}$/;

interface Props {
  plug: UploadPlugInfo;
  threadId: string;
  file: File;
  onDone(handle: string): void;
  onCancel(): void;
  onError(code: string): void;
}

export function parsePlugMessage(
  data: unknown,
  contract: string,
): PlugMessage | null {
  if (typeof data !== "object" || data === null) {
    return null;
  }
  const message = data as Record<string, unknown>;
  if (message.contract !== contract) {
    return null;
  }
  switch (message.type) {
    case "ready":
    case "cancel":
      return { type: message.type, contract };
    case "done":
      if (
        typeof message.thread_id === "string" &&
        typeof message.handle === "string" &&
        HANDLE_PATTERN.test(message.handle)
      ) {
        return {
          type: "done",
          contract,
          thread_id: message.thread_id,
          handle: message.handle,
        };
      }
      return null;
    case "error":
      return {
        type: "error",
        contract,
        code:
          typeof message.code === "string" && CODE_PATTERN.test(message.code)
            ? message.code
            : "PLUG_REJECTED",
      };
    default:
      return null;
  }
}

export default function UploadPlugModal({
  plug,
  threadId,
  file,
  onDone,
  onCancel,
  onError,
}: Props) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const finishedRef = useRef(false);
  const callbacksRef = useRef({ onDone, onCancel, onError });
  callbacksRef.current = { onDone, onCancel, onError };
  const src = `${plug.origin}/embed?thread_id=${encodeURIComponent(
    threadId,
  )}&contract=${encodeURIComponent(plug.contract)}`;

  useEffect(() => {
    finishedRef.current = false;
    let isReady = false;
    const finish = (action: () => void) => {
      if (!finishedRef.current) {
        finishedRef.current = true;
        action();
      }
    };
    const callbacks = () => callbacksRef.current;
    const readyTimer = window.setTimeout(() => {
      if (!isReady) {
        finish(() => callbacks().onError("PLUG_UNREACHABLE"));
      }
    }, READY_TIMEOUT_MS);

    function handleMessage(event: MessageEvent) {
      const frameWindow = frameRef.current?.contentWindow;
      if (event.origin !== plug.origin || !frameWindow || event.source !== frameWindow) {
        return;
      }
      const message = parsePlugMessage(event.data, plug.contract);
      if (!message) {
        return;
      }
      if (message.type === "ready") {
        if (!isReady) {
          isReady = true;
          window.clearTimeout(readyTimer);
          frameWindow.postMessage(
            { type: "file", contract: plug.contract, thread_id: threadId, file },
            plug.origin,
          );
        }
      } else if (message.type === "done") {
        if (message.thread_id === threadId) {
          finish(() => callbacks().onDone(message.handle));
        } else {
          finish(() => callbacks().onError("PLUG_REJECTED"));
        }
      } else if (message.type === "cancel") {
        finish(() => callbacks().onCancel());
      } else {
        finish(() => callbacks().onError(message.code));
      }
    }

    window.addEventListener("message", handleMessage);
    return () => {
      window.clearTimeout(readyTimer);
      window.removeEventListener("message", handleMessage);
    };
  }, [plug.origin, plug.contract, threadId, file]);

  return (
    <div className="upload-plug-backdrop">
      <div
        aria-label="Prepare study upload"
        aria-modal="true"
        className="upload-plug-dialog"
        role="dialog"
      >
        <div className="upload-plug-header">
          <span>Preparing {file.name}</span>
          <button
            onClick={() => {
              if (!finishedRef.current) {
                finishedRef.current = true;
                callbacksRef.current.onCancel();
              }
            }}
            type="button"
          >
            Cancel
          </button>
        </div>
        <iframe
          className="upload-plug-frame"
          ref={frameRef}
          referrerPolicy="no-referrer"
          sandbox="allow-scripts allow-same-origin allow-forms"
          src={src}
          title="Study upload preparation"
        />
      </div>
    </div>
  );
}
