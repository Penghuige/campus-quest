"use client";
/**
 * Structured validation report (spec §12.4; design §9 File upload;
 * patterns §8): errors first, then warnings, then column stats and the
 * data preview. Rendering is layout-only over the pure
 * `validationView` model; preview cells render as PLAIN TEXT (the
 * server guarantees no formulas and no object keys in the report) and
 * long cells arrive pre-truncated from the view.
 *
 * `null` report renders the pending note — the §12.4 payload only rides
 * terminal validation states.
 */
import type { ValidationReportDto } from "./api";
import {
  previewTableView,
  validationReportView,
  type FindingItemView,
} from "./validationView";

export function ValidationReport({ report }: { report: ValidationReportDto }) {
  const view = validationReportView(report);
  return (
    <section className="validation-report" aria-label="校验报告">
      <div className="report-head">
        <h3 className="report-title">校验报告</h3>
        <span className={`badge badge-${view.passed ? "success" : "danger"}`}>
          {view.passed ? "校验通过" : "校验未通过"}
        </span>
        <p className="report-headline">{view.headline}</p>
      </div>

      {view.errors.length > 0 ? (
        <div className="report-group report-errors">
          <h4 className="report-group-title">
            需要修正的问题（{view.errors.length} 项）
          </h4>
          <ul className="report-findings">
            {view.errors.map((finding) => (
              <FindingRow key={finding.key} finding={finding} />
            ))}
          </ul>
        </div>
      ) : null}

      {view.warnings.length > 0 ? (
        <div className="report-group report-warnings">
          <h4 className="report-group-title">提示（{view.warnings.length} 项）</h4>
          <ul className="report-findings">
            {view.warnings.map((finding) => (
              <FindingRow key={finding.key} finding={finding} />
            ))}
          </ul>
        </div>
      ) : null}

      {view.passed && view.warnings.length === 0 ? (
        <p className="report-ok-line">未发现问题，文件已进入人工审核队列。</p>
      ) : null}

      <ColumnStats
        missing={view.columns.missing}
        extra={view.columns.extra}
        typeErrors={view.columns.typeErrors}
        duplicates={view.columns.duplicates}
        nullHeavy={view.columns.nullHeavy}
      />

      {view.preview.rows.length > 0 ? <PreviewTable report={report} /> : null}
    </section>
  );
}

function FindingRow({ finding }: { finding: FindingItemView }) {
  return (
    <li className="report-finding">
      <p>
        {finding.label !== null ? <strong>{finding.label}</strong> : null}
        {finding.label !== null ? "：" : null}
        {finding.message}
      </p>
      <p className="report-finding-meta">
        {finding.location !== null ? <span>{finding.location}</span> : null}
        {finding.sample !== null ? (
          <span className="mono report-sample">{finding.sample}</span>
        ) : null}
      </p>
    </li>
  );
}

function ColumnStats({
  missing,
  extra,
  typeErrors,
  duplicates,
  nullHeavy,
}: {
  missing: string[];
  extra: string[];
  typeErrors: Array<{ column: string; count: number }>;
  duplicates: Array<{ column: string; count: number }>;
  nullHeavy: Array<{ column: string; ratio: string }>;
}) {
  if (
    missing.length === 0 &&
    extra.length === 0 &&
    typeErrors.length === 0 &&
    duplicates.length === 0 &&
    nullHeavy.length === 0
  ) {
    return null;
  }
  return (
    <div className="report-group report-columns">
      <h4 className="report-group-title">列检查</h4>
      <dl className="report-column-facts">
        {missing.length > 0 ? (
          <div className="fact-row">
            <dt className="fact-label">缺少必需列</dt>
            <dd className="fact-value mono">{missing.join("、")}</dd>
          </div>
        ) : null}
        {extra.length > 0 ? (
          <div className="fact-row">
            <dt className="fact-label">多余列</dt>
            <dd className="fact-value mono">{extra.join("、")}</dd>
          </div>
        ) : null}
        {typeErrors.map((item) => (
          <div className="fact-row" key={`type-${item.column}`}>
            <dt className="fact-label">类型不匹配</dt>
            <dd className="fact-value">
              列「{item.column}」共 {item.count} 处
            </dd>
          </div>
        ))}
        {duplicates.map((item) => (
          <div className="fact-row" key={`dup-${item.column}`}>
            <dt className="fact-label">重复值</dt>
            <dd className="fact-value">
              列「{item.column}」共 {item.count} 处
            </dd>
          </div>
        ))}
        {nullHeavy.map((item) => (
          <div className="fact-row" key={`null-${item.column}`}>
            <dt className="fact-label">空值比例较高</dt>
            <dd className="fact-value">
              列「{item.column}」约 {item.ratio}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function PreviewTable({ report }: { report: ValidationReportDto }) {
  const preview = previewTableView(report);
  return (
    <details className="report-preview">
      <summary>数据预览（前 {preview.rows.length} 行）</summary>
      <div className="table-scroll">
        <table className="report-table">
          <tbody>
            {preview.rows.map((row, rowIndex) => (
              // Preview rows have no stable identity; the bounded index is
              // the honest key. Cells render as text only.
              <tr key={rowIndex}>
                {row.map((cell, cellIndex) => (
                  <td key={cellIndex} className="mono report-cell">
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="report-preview-note">
        仅展示部分行列{preview.truncatedCells > 0 ? "，过长内容已截断" : ""}
        {preview.hiddenRows > 0
          ? `，另有 ${preview.hiddenRows} 行未显示`
          : ""}
        。
      </p>
    </details>
  );
}
