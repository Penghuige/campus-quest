/**
 * Task 4: §12.4 validation-report view mapping — every finding class,
 * column aggregates, preview truncation, and the errors-first shape.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import type { ValidationReportDto } from "../features/submissions/api";
import {
  columnStatsView,
  findingItemView,
  NULL_RATIO_DISPLAY_THRESHOLD,
  PREVIEW_CELL_MAX_CHARS,
  previewTableView,
  truncatePreviewCell,
  validationReportView,
} from "../features/submissions/validationView";

function report(overrides: Partial<ValidationReportDto> = {}): ValidationReportDto {
  return {
    parser_version: "csv-1",
    file_type: "CSV",
    row_count: 1234,
    detected_columns: ["platform", "date", "likes"],
    missing_required_columns: [],
    extra_columns: [],
    type_error_counts: {},
    null_ratios: {},
    duplicate_counts: {},
    warnings: [],
    errors: [],
    duration_ms: 12.5,
    preview_rows: [],
    ...overrides,
  };
}

describe("finding view", () => {
  test("a positioned finding renders label + location + sample", () => {
    const view = findingItemView(
      {
        code: "TYPE_ERROR",
        message: "单元格不是数字",
        row: 12,
        column: "likes",
        value: "abc",
      },
      0,
    );
    assert.equal(view.label, "列类型不匹配");
    assert.equal(view.location, "第 12 行 · 列「likes」");
    assert.equal(view.sample, "abc");
    assert.equal(view.key, "TYPE_ERROR-0");
  });

  test("a file-level finding has no location", () => {
    const view = findingItemView(
      { code: "EMPTY_FILE", message: "文件为空", row: null, column: null, value: null },
      3,
    );
    assert.equal(view.label, "文件为空");
    assert.equal(view.location, null);
    assert.equal(view.sample, null);
  });

  test("warning codes carry product framing", () => {
    const truncated = findingItemView(
      { code: "ROW_COUNT_TRUNCATED", message: "行数超过校验上限", row: null, column: null, value: null },
      0,
    );
    assert.equal(truncated.label, "行数统计为下限");
    const degraded = findingItemView(
      { code: "UNIQUE_TRACKING_DEGRADED", message: "唯一性追踪退化", row: null, column: null, value: null },
      0,
    );
    assert.equal(degraded.label, "唯一性校验降级");
  });

  test("an unknown code (registry drift) keeps the server message, label null", () => {
    const view = findingItemView(
      { code: "BRAND_NEW_CODE", message: "新问题", row: null, column: null, value: null },
      0,
    );
    assert.equal(view.label, null);
    assert.equal(view.message, "新问题");
  });
});

describe("column aggregates", () => {
  test("missing/extra pass through; counts sort descending; nulls threshold", () => {
    const stats = columnStatsView(
      report({
        missing_required_columns: ["fans"],
        extra_columns: ["note"],
        type_error_counts: { likes: 2, fans: 9 },
        duplicate_counts: { platform: 1 },
        null_ratios: { date: 0.256, likes: 0.05, fans: 0.9 },
      }),
    );
    assert.deepEqual(stats.missing, ["fans"]);
    assert.deepEqual(stats.extra, ["note"]);
    assert.deepEqual(stats.typeErrors, [
      { column: "fans", count: 9 },
      { column: "likes", count: 2 },
    ]);
    assert.deepEqual(stats.duplicates, [{ column: "platform", count: 1 }]);
    // Only columns at/above the display threshold surface, worst first.
    assert.deepEqual(stats.nullHeavy, [
      { column: "fans", ratio: "90.0%" },
      { column: "date", ratio: "25.6%" },
    ]);
    assert.equal(NULL_RATIO_DISPLAY_THRESHOLD, 0.2);
  });
});

describe("preview table", () => {
  test("long cells truncate with an ellipsis at the bound", () => {
    const long = "x".repeat(PREVIEW_CELL_MAX_CHARS + 10);
    assert.equal(truncatePreviewCell(long).length, PREVIEW_CELL_MAX_CHARS + 1);
    assert.ok(truncatePreviewCell(long).endsWith("…"));
    assert.equal(truncatePreviewCell("short"), "short");
  });

  test("the view counts truncated cells and withheld rows", () => {
    const longCell = "y".repeat(100);
    const rows = Array.from({ length: 12 }, () => ["a", longCell]);
    const view = previewTableView(report({ preview_rows: rows }));
    assert.equal(view.rows.length, 10);
    assert.equal(view.hiddenRows, 2);
    assert.equal(view.truncatedCells, 10);
    assert.ok(view.rows[0]![1]!.endsWith("…"));
  });

  test("cells render as plain data — nothing is transformed beyond truncation", () => {
    const view = previewTableView(report({ preview_rows: [["=SUM(A1)", "1\t2"]] }));
    assert.equal(view.rows[0]![0], "=SUM(A1)");
    assert.equal(view.rows[0]![1], "1\t2");
  });
});

describe("whole-report view", () => {
  test("headline carries the exact row count and file type", () => {
    const view = validationReportView(report());
    assert.equal(view.headline, "共 1,234 行 · CSV");
    assert.equal(view.passed, true);
  });

  test("errors present -> not passed; errors and warnings map in order", () => {
    const view = validationReportView(
      report({
        errors: [
          { code: "MISSING_REQUIRED_COLUMN", message: "缺少 fans", row: null, column: null, value: null },
        ],
        warnings: [
          { code: "ROW_COUNT_TRUNCATED", message: "行数为下限", row: null, column: null, value: null },
        ],
      }),
    );
    assert.equal(view.passed, false);
    assert.equal(view.errors.length, 1);
    assert.equal(view.warnings.length, 1);
    assert.equal(view.errors[0]!.label, "缺少必需列");
  });
});
