// @vitest-environment jsdom
/**
 * The run report renders only what was measured.
 *
 * - **B-F02** — `report` used to default to `MOCK_RUN_REPORT`. Omitting the prop
 *   rendered a different repository's trust score, file list and PR decision as
 *   though they belonged to this run. The prop is now required, which makes the
 *   defect a compile error rather than a test; what is tested here is the
 *   behaviour that replaced it — an unmeasured report says so.
 * - **B-F05** — a blocked run's decision badge used to borrow the draft tone.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { RunReport } from "./RunReport";
import { EMPTY_RUN_REPORT } from "./emptyModels";

describe("RunReport trust score", () => {
  it("reports an unmeasured trust score as unmeasured, never as a number", () => {
    render(<RunReport done report={{ ...EMPTY_RUN_REPORT, trustScore: null }} />);

    expect(screen.getByText("Not measured")).toBeTruthy();
    // The threshold comparison is a verdict; a run with no measurement has not
    // reached one, so neither phrasing may appear.
    expect(screen.queryByText(/Confidence meets threshold/)).toBeNull();
    expect(screen.queryByText(/Confidence below threshold/)).toBeNull();
    expect(
      screen.getByText(/No axis was measured, so confidence could not be established/),
    ).toBeTruthy();
  });

  it("renders a real score against its threshold", () => {
    render(
      <RunReport done report={{ ...EMPTY_RUN_REPORT, trustScore: 0.91, trustThreshold: 0.9 }} />,
    );

    expect(screen.queryByText("Not measured")).toBeNull();
    expect(screen.getByText(/Confidence meets threshold/)).toBeTruthy();
  });

  it("renders a measured zero as a score, not as an absence", () => {
    render(<RunReport done report={{ ...EMPTY_RUN_REPORT, trustScore: 0, trustThreshold: 0.9 }} />);

    expect(screen.queryByText("Not measured")).toBeNull();
    expect(screen.getByText(/Confidence below threshold/)).toBeTruthy();
  });
});

describe("RunReport decision badge", () => {
  it("gives a blocked run its own badge rather than the draft one", () => {
    render(
      <RunReport
        done
        report={{
          ...EMPTY_RUN_REPORT,
          decision: "blocked",
          decisionLabel: "Environment not prepared",
        }}
      />,
    );

    const badge = screen.getByText("Environment not prepared");
    expect(badge.className).toContain("status-blocked");
    expect(badge.className).not.toContain("status-draft");
  });
});

describe("Draft reasons", () => {
  it("lists every reason, not just the one A10 noted", () => {
    // `decisionReason` is A10's `review_note`, which carries the first reason
    // routing hit. A run blocked for three reasons showed one and the rest were
    // unrecoverable from the UI.
    render(
      <RunReport
        done
        report={{
          ...EMPTY_RUN_REPORT,
          decisionReason: "Validation retries exhausted. Manual verification required.",
          draftReasons: [
            {
              code: "validation_exhausted",
              detail: "Validation retries exhausted without a patch that passed.",
            },
            {
              code: "citations_unverified",
              detail: "Citation verification incomplete after the maximum reinvestigations.",
            },
            {
              code: "reproduction_no_tests",
              detail: "A3.5 Reproduction Gate: no tests available to confirm the vulnerability.",
            },
          ],
        }}
      />,
    );

    expect(screen.getByText(/Why this is a draft \(3\)/)).toBeTruthy();
    expect(screen.getByText(/no tests available to confirm/)).toBeTruthy();
    expect(screen.getByText(/Citation verification incomplete/)).toBeTruthy();
  });

  it("renders the backend's sentences verbatim", () => {
    const detail = "A3.5 Reproduction Gate: bug could not be reproduced in test environment.";
    render(
      <RunReport
        done
        report={{
          ...EMPTY_RUN_REPORT,
          draftReasons: [{ code: "reproduction_unconfirmed", detail }],
        }}
      />,
    );

    expect(screen.getByText(detail)).toBeTruthy();
    // Singular heading — the count is only shown when there is more than one.
    expect(screen.getByText("Why this is a draft")).toBeTruthy();
  });

  it("shows nothing for a run with no draft reasons", () => {
    render(<RunReport done report={{ ...EMPTY_RUN_REPORT, draftReasons: [] }} />);

    expect(screen.queryByText(/Why this is a draft/)).toBeNull();
  });

  it("shows nothing when the backend predates the field", () => {
    render(<RunReport done report={EMPTY_RUN_REPORT} />);

    expect(screen.queryByText(/Why this is a draft/)).toBeNull();
  });
});

describe("RunReport export and download functionality", () => {
  it("getRunReportFilename formats and sanitizes repository and runId", async () => {
    const { getRunReportFilename } = await import("./RunReport");

    expect(
      getRunReportFilename({
        ...EMPTY_RUN_REPORT,
        repository: "demo-vln",
        shortRunId: "4e55-4457",
      }),
    ).toBe("demo-vln-run-report-4e55-4457.json");

    expect(
      getRunReportFilename({
        ...EMPTY_RUN_REPORT,
        repository: "org/repo:name",
        shortRunId: "run#123",
      }),
    ).toBe("org-repo-name-run-report-run-123.json");

    expect(
      getRunReportFilename({
        ...EMPTY_RUN_REPORT,
        repository: "",
        shortRunId: "",
        runId: "",
      }),
    ).toBe("run-report.json");
  });

  it("serializeRunReport exports the full report object preserving nested fields", async () => {
    const { serializeRunReport } = await import("./RunReport");
    const testReport = {
      ...EMPTY_RUN_REPORT,
      repository: "DemoRepo",
      decision: "draft" as const,
      rootCause: {
        summary: "Full analysis summary",
        statement: "Defect statement",
        location: "backend/main.py:10",
        claim: "Claim text",
        verified: true,
      },
      files: ["backend/main.py", "frontend/src/App.tsx"],
    };

    const json = serializeRunReport(testReport);
    const parsed = JSON.parse(json);

    expect(parsed.repository).toBe("DemoRepo");
    expect(parsed.rootCause.summary).toBe("Full analysis summary");
    expect(parsed.rootCause.verified).toBe(true);
    expect(parsed.files).toEqual(["backend/main.py", "frontend/src/App.tsx"]);
  });

  it("copies complete JSON to clipboard and updates button to Copied", async () => {
    const { fireEvent, act } = await import("@testing-library/react");
    const writeTextMock = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, {
      clipboard: {
        writeText: writeTextMock,
      },
    });

    const testReport = {
      ...EMPTY_RUN_REPORT,
      repository: "test-repo",
      shortRunId: "abc-123",
    };

    render(<RunReport done report={testReport} />);

    const copyBtn = screen.getByRole("button", { name: /Copy JSON/i });
    expect(copyBtn).toBeTruthy();

    await act(async () => {
      fireEvent.click(copyBtn);
    });

    expect(writeTextMock).toHaveBeenCalledTimes(1);
    const copiedText = writeTextMock.mock.calls[0][0];
    const parsed = JSON.parse(copiedText);
    expect(parsed.repository).toBe("test-repo");
    expect(parsed.shortRunId).toBe("abc-123");

    // UI should show "Copied"
    expect(screen.getByText("Copied")).toBeTruthy();
  });

  it("triggers browser download with sanitized filename and cleans up object URL", async () => {
    const { fireEvent, act } = await import("@testing-library/react");
    const createObjectURLMock = vi.fn().mockReturnValue("blob:http://localhost/test-blob");
    const revokeObjectURLMock = vi.fn();
    global.URL.createObjectURL = createObjectURLMock;
    global.URL.revokeObjectURL = revokeObjectURLMock;

    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    const testReport = {
      ...EMPTY_RUN_REPORT,
      repository: "my-app",
      shortRunId: "xyz-789",
    };

    render(<RunReport done report={testReport} />);

    const downloadBtn = screen.getByRole("button", { name: /Download/i });
    expect(downloadBtn).toBeTruthy();

    await act(async () => {
      fireEvent.click(downloadBtn);
    });

    expect(createObjectURLMock).toHaveBeenCalledTimes(1);
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectURLMock).toHaveBeenCalledWith("blob:http://localhost/test-blob");

    clickSpy.mockRestore();
  });
});
