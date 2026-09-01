import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import DatasetSchemaView from "./DatasetSchemaView";

describe("DatasetSchemaView", () => {
  it("renders catalog metadata in a human-readable table", () => {
    render(
      <DatasetSchemaView
        schema={{
          baseline_sex: {
            dataType: "object",
            description: "Sex recorded at baseline",
            values: { F: "Female", M: "Male" },
            section_context: "Baseline characteristics",
          },
        }}
      />,
    );

    const table = screen.getByRole("table", { name: "Dataset schema" });
    expect(within(table).getByText("baseline_sex")).toBeInTheDocument();
    expect(
      within(table).getByText("Sex recorded at baseline"),
    ).toBeInTheDocument();
    expect(within(table).getByText("object")).toBeInTheDocument();
    expect(within(table).getByText("F — Female")).toBeInTheDocument();
    expect(within(table).getByText("M — Male")).toBeInTheDocument();
    expect(
      within(table).getByText("Section: Baseline characteristics"),
    ).toBeInTheDocument();
    expect(screen.getByText("Raw schema").closest("details")).not.toHaveAttribute(
      "open",
    );
  });

  it("keeps historical dtype-only schemas readable without enriching them", () => {
    render(
      <DatasetSchemaView
        schema={{ participant_id: { dataType: "object" } }}
      />,
    );

    const table = screen.getByRole("table", { name: "Dataset schema" });
    expect(within(table).getByText("participant_id")).toBeInTheDocument();
    expect(within(table).getByText("Description unavailable")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Raw schema"));
    expect(screen.getByText(/"dataType": "object"/)).toBeInTheDocument();
  });
});
