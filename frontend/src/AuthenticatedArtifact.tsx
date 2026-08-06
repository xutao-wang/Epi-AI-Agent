import { useEffect, useState } from "react";

interface Props {
  alt: string;
  filename: string;
  load: () => Promise<Blob>;
  mode: "download" | "image";
}

type Status = "error" | "loading" | "ready";

export default function AuthenticatedArtifact({
  alt,
  filename,
  load,
  mode,
}: Props) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [status, setStatus] = useState<Status>("loading");

  useEffect(() => {
    let active = true;
    let nextUrl: string | null = null;
    setStatus("loading");
    load()
      .then((blob) => {
        if (!active) return;
        nextUrl = URL.createObjectURL(blob);
        setObjectUrl(nextUrl);
        setStatus("ready");
      })
      .catch(() => {
        if (active) setStatus("error");
      });
    return () => {
      active = false;
      if (nextUrl) URL.revokeObjectURL(nextUrl);
    };
  }, [load]);

  if (status === "loading") {
    return <span aria-live="polite">Loading {alt}…</span>;
  }
  if (status === "error" || !objectUrl) {
    return <span role="alert">Unable to load {alt}.</span>;
  }
  if (mode === "image") {
    return <img alt={alt} src={objectUrl} />;
  }

  return (
    <button
      onClick={() => {
        const anchor = document.createElement("a");
        anchor.download = filename;
        anchor.href = objectUrl;
        anchor.click();
        anchor.remove();
      }}
      type="button"
    >
      Download {filename}
    </button>
  );
}
