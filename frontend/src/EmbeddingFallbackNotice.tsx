import type { EmbeddingStartupStatus } from "./types";


interface Props {
  status: EmbeddingStartupStatus | null | undefined;
}


export function EmbeddingFallbackNotice({ status }: Props) {
  const message = status?.message.trim() ?? "";
  if (!message) {
    return null;
  }
  return (
    <section className="embedding-fallback-notice" role="status">
      {message}
    </section>
  );
}


export default EmbeddingFallbackNotice;
