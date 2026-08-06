import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AuthenticatedArtifact from "./AuthenticatedArtifact";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("AuthenticatedArtifact", () => {
  it("creates and revokes an object URL for an authenticated image", async () => {
    const load = vi.fn().mockResolvedValue(new Blob(["image"], { type: "image/png" }));
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:authenticated-image");
    const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);

    const { unmount } = render(
      <AuthenticatedArtifact alt="result" load={load} mode="image" filename="result.png" />,
    );
    expect(await screen.findByAltText("result")).toHaveAttribute(
      "src",
      "blob:authenticated-image",
    );
    unmount();
    expect(revoke).toHaveBeenCalledWith("blob:authenticated-image");
  });
});
