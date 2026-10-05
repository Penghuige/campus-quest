/**
 * Dev-only component gallery (plan-12 task 7): the shared visual
 * primitive × variant matrix on ONE page, so design review and the
 * task-9 pixel baseline can inspect the current visual contract without
 * driving the seeded app through every surface.
 *
 * Render rules (the page's reason to exist is fidelity, not novelty):
 * - it composes ONLY existing global classes (globals.css) and shared
 *   components (TaskCard, the sectionStates primitives, navIcons
 *   glyphs) — no gallery-local styling exists or may be added (inline
 *   controls are separated by plain markup whitespace, not CSS);
 * - fixture data is inline and fully deterministic (no clocks, no
 *   randomness) so the pixel baseline is stable.
 *
 * Production gate: the route 404s outside development. The mechanism is
 * `notFound()` (bundled Next 16.3.5 docs,
 * node_modules/next/dist/docs/01-app/03-api-reference/04-functions/not-found.md:
 * "Invoking notFound() throws a NEXT_HTTP_ERROR_FALLBACK;404 error and
 * terminates rendering of the route segment"), keyed on NODE_ENV. `next
 * build` also runs with NODE_ENV=production, so the prerender pass
 * bakes the not-found page in as the route's static output — the
 * production artifact itself carries the 404, there is no publicly
 * reachable gallery to ship. Verified on HEAD via `npm run build` +
 * `next start` + curl (404).
 *
 * No `export const dynamic` override: the bundled route-segment-config
 * reference (…/03-file-conventions/02-route-segment-config/index.md)
 * lists `dynamic` among the options REMOVED once Cache Components is
 * enabled, so pinning the legacy flag would only buy a future removal;
 * the default ('auto') already prerenders this static page through the
 * env gate above.
 */
import { notFound } from "next/navigation";
import { Fragment, type ReactNode } from "react";

import {
  RarityEpicIcon,
  RarityLegendaryIcon,
  RarityNormalIcon,
  RarityRareIcon,
} from "@/components/shell/navIcons";
import {
  EmptyState,
  SectionError,
  SectionSkeleton,
} from "@/components/ui/sectionStates";
import { TaskCard } from "@/features/tasks/TaskCard";
import type { TaskCardDto } from "@/features/tasks/api";
import { rarityView, type RarityKey } from "@/features/tasks/display";

const RARITIES: readonly RarityKey[] = ["NORMAL", "RARE", "EPIC", "LEGENDARY"];

const RARITY_GLYPHS = {
  NORMAL: RarityNormalIcon,
  RARE: RarityRareIcon,
  EPIC: RarityEpicIcon,
  LEGENDARY: RarityLegendaryIcon,
} as const;

/** Fixed card fixtures: rarity is the axis under review; RELATIVE deadline
 * mode keeps the countdown line a pure function of the fixture. */
const FIXTURE_TASKS: readonly TaskCardDto[] = RARITIES.map((rarity, index) => ({
  id: `00000000-0000-4000-8000-00000000000${index + 1}`,
  title: `示例任务标题（${rarityView(rarity).label}）`,
  rarity,
  base_reward_points: [40, 80, 160, 260][index] ?? 40,
  deadline_mode: "RELATIVE",
  fixed_deadline_at: null,
  duration_minutes: 4320,
  assignments_available: 3 - index,
  rating: null,
}));

/** TaskCard's display-only island clock; fixed so the render is pure. */
const FIXED_NOW_MS = 1_800_000_000_000;

const BUTTON_VARIANTS = [
  { className: "btn btn-primary", label: "主要操作" },
  { className: "btn btn-secondary", label: "次要操作" },
  { className: "btn btn-ghost", label: "轻量操作" },
  { className: "btn btn-danger", label: "危险操作" },
] as const;

const STATUS_BADGES = [
  { className: "badge badge-info", label: "待审核" },
  { className: "badge badge-success", label: "已完成" },
  { className: "badge badge-warning", label: "需修改" },
  { className: "badge badge-danger", label: "已截止" },
  { className: "badge badge-muted", label: "已过期" },
] as const;

/** Join inline controls with one plain space (markup, not styling). */
function spaced(nodes: readonly ReactNode[]): ReactNode {
  return nodes.map((node, index) => (
    // Static fixture lists never reorder; the positional key is stable.
    <Fragment key={index}>
      {index > 0 ? " " : null}
      {node}
    </Fragment>
  ));
}

export default function DevGalleryPage() {
  if (process.env.NODE_ENV === "production") {
    notFound();
  }
  return (
    <main className="app-main">
      <header className="page-head">
        <h1 className="page-title">组件画廊</h1>
        <p className="page-subtitle">
          共享视觉原语 × 变体矩阵 — 仅开发环境可见，用于设计走查与像素基线。
        </p>
      </header>

      <section className="section" aria-label="按钮">
        <h2 className="section-title">按钮</h2>
        <p>
          {spaced(
            BUTTON_VARIANTS.map((variant) => (
              <button key={variant.className} type="button" className={variant.className}>
                {variant.label}
              </button>
            )),
          )}
        </p>
        <p>
          {spaced(
            BUTTON_VARIANTS.map((variant) => (
              <button
                key={variant.className}
                type="button"
                className={variant.className}
                disabled
              >
                {variant.label}（禁用）
              </button>
            )),
          )}
        </p>
      </section>

      <section className="section" aria-label="徽章与稀有度">
        <h2 className="section-title">状态徽章</h2>
        <p>
          {spaced(
            STATUS_BADGES.map((badge) => (
              <span key={badge.className} className={badge.className}>
                {badge.label}
              </span>
            )),
          )}
        </p>
        <h2 className="section-title">稀有度（卡片徽章）</h2>
        <p>
          {spaced(
            RARITIES.map((rarity) => {
              const Glyph = RARITY_GLYPHS[rarity];
              return (
                <span key={rarity} className="task-card-rarity" data-rarity={rarity}>
                  <Glyph />
                  {rarityView(rarity).label}
                </span>
              );
            }),
          )}
        </p>
        <h2 className="section-title">稀有度（详情徽章）</h2>
        <p>
          {spaced(
            RARITIES.map((rarity) => (
              <span key={rarity} className="rarity-badge" data-rarity={rarity}>
                {rarityView(rarity).label}
              </span>
            )),
          )}
        </p>
      </section>

      <section className="section" aria-label="任务卡片">
        <h2 className="section-title">任务卡片 × 稀有度</h2>
        <div className="task-grid">
          {FIXTURE_TASKS.map((card) => (
            <TaskCard key={card.id} card={card} nowMs={FIXED_NOW_MS} />
          ))}
        </div>
      </section>

      <section className="section" aria-label="表单控件">
        <h2 className="section-title">表单控件</h2>
        <div className="field">
          <label className="field-label" htmlFor="gallery-input-normal">
            标准输入
          </label>
          <input
            id="gallery-input-normal"
            className="input"
            defaultValue="已填写的内容"
            readOnly
          />
          <p className="field-hint">帮助文本说明后果，而不是复述字段名。</p>
        </div>
        <div className="field">
          <label className="field-label" htmlFor="gallery-input-error">
            错误状态
          </label>
          <input
            id="gallery-input-error"
            className="input"
            defaultValue=""
            placeholder="请输入内容"
            aria-invalid="true"
            aria-describedby="gallery-input-error-message"
            readOnly
          />
          <p className="field-error" id="gallery-input-error-message">
            内容不能为空
          </p>
        </div>
        <div className="field">
          <label className="field-label" htmlFor="gallery-textarea-disabled">
            禁用状态
          </label>
          <textarea
            id="gallery-textarea-disabled"
            className="input"
            rows={2}
            defaultValue="禁用控件保持可读"
            disabled
          />
        </div>
      </section>

      <section className="section" aria-label="区块状态">
        <h2 className="section-title">加载 / 空 / 错误</h2>
        <SectionSkeleton lines={3} />
        <EmptyState title="暂无通知" hint="任务与审核的进展会出现在这里" />
        <SectionError error={new Error("gallery fixture")} />
      </section>

      <section className="section" aria-label="审核队列行">
        <h2 className="section-title">审核队列行（默认 / 选中）</h2>
        {(["false", "true"] as const).map((selected) => (
          <button
            key={selected}
            type="button"
            className="review-item"
            aria-pressed={selected === "true"}
            data-selected={selected}
          >
            <span className="review-item-head">
              <strong className="review-item-title">示例任务标题</strong>
              <span className="mono review-pair">xiaohongshu / 示例关键词</span>
            </span>
            <span className="review-item-meta">
              <span className="badge badge-info">待审核</span>
              <span className="badge badge-success">校验通过</span>
              <span className="meta-num review-tier">100% 档位</span>
            </span>
          </button>
        ))}
      </section>

      {/* LAST section by construction: a non-modal `<dialog open>` keeps
          the UA's absolute centering (the .dialog class adds no position),
          so it paints out of flow — at the page end it overlays nothing. */}
      <section className="section" aria-label="对话框">
        <h2 className="section-title">对话框（打开状态）</h2>
        <dialog className="dialog" open aria-labelledby="gallery-dialog-title">
          <div className="dialog-body">
            <h3 id="gallery-dialog-title" className="dialog-title">
              确认操作
            </h3>
            <p>对话框正文沿用全局 .dialog 排版。</p>
            <div className="dialog-actions">
              <button type="button" className="btn btn-ghost">
                取消
              </button>
              <button type="button" className="btn btn-primary">
                完成
              </button>
            </div>
          </div>
        </dialog>
      </section>
    </main>
  );
}
