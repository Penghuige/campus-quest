/**
 * Pure presentation mapping for the §12.4 validation report (spec §12.4;
 * design §9 File upload; patterns §8 — errors first, then warnings, then
 * successful checks).
 *
 * Everything here is a pure function of the SERVER report plus explicit
 * bounds — no hidden state, so every finding class and the preview
 * truncation are deterministically testable. The report arrives
 * object-key-free and formula-free BY CONSTRUCTION (backend
 * `ValidationReportPayload`); preview cells render as PLAIN TEXT — the
 * server guarantees no formula payloads, and nothing here re-parses cell
 * content (spec §12.4 preview contract).
 */
import type { ValidationFindingDto, ValidationReportDto } from "./api";

// --- findings ----------------------------------------------------------------------

/** One rendered error/warning line. */
export interface FindingItemView {
  /** Stable React key (finding code + index; codes repeat across rows). */
  key: string;
  /** Product framing for known codes; null renders the server message alone. */
  label: string | null;
  /** The server's own message — display material, never parsed. */
  message: string;
  /** "第 12 行 · 列 「粉丝数」" when the sample carries a position. */
  location: string | null;
  /** The offending value sample, verbatim (bounded server-side). */
  sample: string | null;
}

/**
 * Product framing per known §12.4 finding code (the shared
 * `ValidationCode` registry; anything unknown renders the server message
 * alone — registry drift must not blank a finding).
 */
const FINDING_LABELS: Record<string, string> = {
  MISSING_REQUIRED_COLUMN: "缺少必需列",
  EXTRA_COLUMN: "存在多余列",
  TYPE_ERROR: "列类型不匹配",
  DUPLICATE_VALUE: "存在重复值",
  NULL_VIOLATION: "必填单元格为空",
  NULL_RATIO_EXCEEDED: "空值比例过高",
  ROW_SHAPE_MISMATCH: "行结构与表头不一致",
  NO_DATA_ROWS: "没有数据行",
  EMPTY_FILE: "文件为空",
  MIN_ROWS_NOT_MET: "行数未达到最少要求",
  MAX_ROWS_EXCEEDED: "行数超出上限",
  ROW_LIMIT_EXCEEDED: "行数超出上限",
  CELL_TOO_LONG: "单元格内容过长",
  TOO_MANY_COLUMNS: "列数过多",
  DUPLICATE_HEADER: "表头列名重复",
  INVALID_ENCODING: "文件编码无法识别",
  MALFORMED_CSV: "CSV 格式错误",
  MALFORMED_XLSX: "Excel 文件无法解析",
  MALFORMED_SQLITE: "SQLite 文件无法解析",
  SHEET_NOT_FOUND: "找不到要求的工作表",
  TABLE_NOT_FOUND: "找不到要求的数据表",
  AMBIGUOUS_TABLE: "无法确定目标数据表",
  ARCHIVE_TOO_LARGE: "压缩包过大",
  PART_TOO_LARGE: "压缩包内文件过大",
  SUSPICIOUS_COMPRESSION_RATIO: "压缩比异常",
  SUSPICIOUS_ARCHIVE_ENTRY: "压缩包条目异常",
  BINARY_CONTENT: "内容不是预期的文本格式",
  EXTERNAL_LINK: "存在外部链接",
  FORMULA_WITHOUT_CACHED_VALUE: "公式没有缓存结果",
  UNIQUE_TRACKING_DEGRADED: "唯一性校验降级",
  ROW_COUNT_TRUNCATED: "行数统计为下限",
  FINDINGS_TRUNCATED: "问题列表已截断",
  TIMEOUT: "校验超时",
  VALIDATION_TIMED_OUT: "校验超时",
  VALIDATION_WORKER_CRASHED: "校验服务异常",
  FILE_TYPE_NOT_ALLOWED: "文件类型不允许",
  SCHEMA_INVALID: "任务校验配置异常",
};

/** Map one finding sample to its rendered line. Pure. */
export function findingItemView(
  finding: ValidationFindingDto,
  index: number,
): FindingItemView {
  const locationParts: string[] = [];
  if (finding.row !== null && finding.row !== undefined) {
    locationParts.push(`第 ${finding.row} 行`);
  }
  if (finding.column !== null && finding.column !== undefined) {
    locationParts.push(`列「${finding.column}」`);
  }
  return {
    key: `${finding.code}-${index}`,
    label: FINDING_LABELS[finding.code] ?? null,
    message: finding.message,
    location: locationParts.length > 0 ? locationParts.join(" · ") : null,
    sample: finding.value ?? null,
  };
}

// --- column-level aggregates -------------------------------------------------------

export interface ColumnStatsView {
  missing: string[];
  extra: string[];
  typeErrors: Array<{ column: string; count: number }>;
  duplicates: Array<{ column: string; count: number }>;
  /** Columns whose null ratio crosses a display threshold (top offenders). */
  nullHeavy: Array<{ column: string; ratio: string }>;
}

/** Ratio (0..1) above which a column's null ratio is worth surfacing. */
export const NULL_RATIO_DISPLAY_THRESHOLD = 0.2;

/**
 * Column-level aggregates from the report's per-column dicts, sorted
 * count-descending for stable, dense presentation. Pure.
 */
export function columnStatsView(report: ValidationReportDto): ColumnStatsView {
  const byCount = (a: [string, number], b: [string, number]) => b[1] - a[1];
  return {
    missing: [...report.missing_required_columns],
    extra: [...report.extra_columns],
    typeErrors: Object.entries(report.type_error_counts)
      .sort(byCount)
      .map(([column, count]) => ({ column, count })),
    duplicates: Object.entries(report.duplicate_counts)
      .sort(byCount)
      .map(([column, count]) => ({ column, count })),
    nullHeavy: Object.entries(report.null_ratios)
      .filter(([, ratio]) => ratio >= NULL_RATIO_DISPLAY_THRESHOLD)
      .sort((a, b) => b[1] - a[1])
      .map(([column, ratio]) => ({
        column,
        ratio: `${(ratio * 100).toFixed(1)}%`,
      })),
  };
}

// --- preview table -----------------------------------------------------------------

/** Preview cell bound: beyond this a cell renders cut with an ellipsis. */
export const PREVIEW_CELL_MAX_CHARS = 60;
/** Preview row bound for the rendered table (report rows are already bounded). */
export const PREVIEW_MAX_ROWS = 10;

export interface PreviewTableView {
  rows: string[][];
  /** Cells shortened to `PREVIEW_CELL_MAX_CHARS` + ellipsis. */
  truncatedCells: number;
  /** Rows withheld by the `PREVIEW_MAX_ROWS` cap. */
  hiddenRows: number;
}

/** Cut one preview cell for display (pure text; the server guarantees no formulas). */
export function truncatePreviewCell(cell: string): string {
  return cell.length > PREVIEW_CELL_MAX_CHARS
    ? `${cell.slice(0, PREVIEW_CELL_MAX_CHARS)}…`
    : cell;
}

/**
 * Build the preview table view: plain-text cells, per-cell truncation,
 * row cap with an explicit withheld count. Pure.
 */
export function previewTableView(report: ValidationReportDto): PreviewTableView {
  const shown = report.preview_rows.slice(0, PREVIEW_MAX_ROWS);
  let truncatedCells = 0;
  const rows = shown.map((row) =>
    row.map((cell) => {
      const truncated = truncatePreviewCell(cell);
      if (truncated !== cell) {
        truncatedCells += 1;
      }
      return truncated;
    }),
  );
  return {
    rows,
    truncatedCells,
    hiddenRows: Math.max(report.preview_rows.length - shown.length, 0),
  };
}

// --- whole-report summary ----------------------------------------------------------

export interface ValidationReportView {
  /** "共 1,234 行 · CSV" — counts stay exact (§12.4). */
  headline: string;
  /** True when the report carries no errors (validation passed). */
  passed: boolean;
  errors: FindingItemView[];
  warnings: FindingItemView[];
  columns: ColumnStatsView;
  preview: PreviewTableView;
}

const FILE_TYPE_LABELS: Record<string, string> = {
  CSV: "CSV",
  XLSX: "Excel",
  SQLITE: "SQLite",
};

/**
 * Whole-report view. Errors and warnings keep the server's ordering
 * (already bounded + severity-ordered by the report builder); rendering
 * puts errors first regardless (patterns §8).
 */
export function validationReportView(
  report: ValidationReportDto,
): ValidationReportView {
  const fileLabel = FILE_TYPE_LABELS[report.file_type] ?? report.file_type;
  return {
    headline: `共 ${report.row_count.toLocaleString("zh-CN")} 行 · ${fileLabel}`,
    passed: report.errors.length === 0,
    errors: report.errors.map(findingItemView),
    warnings: report.warnings.map(findingItemView),
    columns: columnStatsView(report),
    preview: previewTableView(report),
  };
}
