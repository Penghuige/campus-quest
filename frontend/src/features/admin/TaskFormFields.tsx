"use client";
/**
 * The shared task form fields (create + edit, spec §6/§6.2; patterns §6;
 * the 2026-10-23 simplification — spec 2026-10-23-simplification-design
 * §3.1/§3.2).
 *
 * Layout only: values/errors/disabled state arrive by props; submission
 * and error mapping stay the dialogs' (the pure band rules live in
 * `validateTaskForm`).
 *
 * The SIMPLIFIED shape: the owner path asks four business questions in
 * the open — 任务标题, 任务描述, 需要多少人完成 (target_count), 截止
 * (days, seven by default). Everything else (reward, rarity, the
 * deadline-mode switch, cutoff, size, file types, the OPTIONAL schema)
 * rides the collapsed 高级 section carrying working defaults. The
 * schema starts EMPTY (lenient validation is the default; strict is the
 * explicitly-opened option — the 使用模板 button offers a starting
 * point, never a requirement).
 *
 * `frozen` carries the V1 edit-rule contract fields (frozen from first
 * publish): a frozen input renders disabled with the 发布后不可修改
 * hint — an affordance mirror only; the server's
 * `ImmutableTaskFieldError` envelope (VALIDATION_ERROR + details.fields)
 * remains the verdict and renders in the owning dialog.
 */
import {
  ALLOWED_TASK_FILE_TYPES,
  MAX_TASK_FILE_SIZE_BYTES,
} from "./teacherApi";
import {
  DEADLINE_MODE_OPTIONS,
  daysToMinutesInput,
  defaultSchemaFor,
  minutesToDaysInput,
  TASK_NOTIFICATION_CHANNEL_OPTIONS,
  TASK_RARITY_OPTIONS,
  type TaskFormErrors,
  type TaskFormValues,
} from "./teacherView";

/** Every editable form key (validation bands key off this union). */
export type TaskFormFieldKey = keyof TaskFormValues;

export interface TaskFormFieldsProps {
  values: TaskFormValues;
  fieldErrors: TaskFormErrors;
  /** Whole-form disable (a submission is in flight). */
  disabled: boolean;
  /** Fields frozen by the edit rule; rendered read-only with the hint. */
  frozen?: ReadonlySet<TaskFormFieldKey>;
  setField: <K extends TaskFormFieldKey>(key: K, value: TaskFormValues[K]) => void;
  /** The edit surface omits 需要多少人完成 (units already exist; the
   * PATCH never carries target_count). */
  hideTargetCount?: boolean;
}

const FROZEN_HINT = "已发布后不可修改";

export function TaskFormFields({
  values,
  fieldErrors,
  disabled,
  frozen,
  setField,
  hideTargetCount = false,
}: TaskFormFieldsProps) {
  const isFrozen = (key: TaskFormFieldKey): boolean => frozen?.has(key) ?? false;
  const lock = (key: TaskFormFieldKey): boolean => disabled || isFrozen(key);

  return (
    <>
      <div className="field">
        <label className="field-label" htmlFor="task-title">
          任务标题
        </label>
        <input
          id="task-title"
          className="input"
          value={values.title}
          onChange={(event) => setField("title", event.target.value)}
          maxLength={255}
          required
          aria-invalid={fieldErrors.title !== undefined}
          disabled={lock("title")}
        />
        {fieldErrors.title !== undefined ? (
          <p className="field-error">{fieldErrors.title}</p>
        ) : null}
      </div>

      <div className="field">
        <label className="field-label" htmlFor="task-description">
          任务描述
        </label>
        <textarea
          id="task-description"
          className="input"
          rows={3}
          value={values.description}
          onChange={(event) => setField("description", event.target.value)}
          required
          aria-invalid={fieldErrors.description !== undefined}
          disabled={lock("description")}
        />
        {fieldErrors.description !== undefined ? (
          <p className="field-error">{fieldErrors.description}</p>
        ) : null}
      </div>

      <div className="form-grid">
        {hideTargetCount ? null : (
          <div className="field">
            <label className="field-label" htmlFor="task-target-count">
              需要多少人完成
            </label>
            <input
              id="task-target-count"
              className="input"
              inputMode="numeric"
              placeholder="1-200"
              value={values.targetCount}
              onChange={(event) => setField("targetCount", event.target.value)}
              required
              aria-invalid={fieldErrors.targetCount !== undefined}
              disabled={lock("targetCount")}
            />
            <p className="field-hint">创建后自动生成对应数量的任务单元（通用平台）</p>
            {fieldErrors.targetCount !== undefined ? (
              <p className="field-error">{fieldErrors.targetCount}</p>
            ) : null}
          </div>
        )}

        {values.deadlineMode === "RELATIVE" ? (
          <div className="field">
            <label className="field-label" htmlFor="task-duration-days">
              领取后截止（天）
            </label>
            <input
              id="task-duration-days"
              className="input"
              inputMode="decimal"
              value={minutesToDaysInput(values.durationMinutes)}
              onChange={(event) =>
                setField("durationMinutes", daysToMinutesInput(event.target.value))
              }
              required
              aria-invalid={fieldErrors.deadline !== undefined}
              disabled={lock("durationMinutes")}
            />
            {fieldErrors.deadline !== undefined ? (
              <p className="field-error">{fieldErrors.deadline}</p>
            ) : null}
          </div>
        ) : (
          <div className="field">
            <label className="field-label" htmlFor="task-deadline">
              固定截止时间
            </label>
            <input
              id="task-deadline"
              className="input"
              type="datetime-local"
              value={values.fixedDeadlineLocal}
              onChange={(event) => setField("fixedDeadlineLocal", event.target.value)}
              required
              aria-invalid={fieldErrors.deadline !== undefined}
              disabled={lock("fixedDeadlineLocal")}
            />
            {fieldErrors.deadline !== undefined ? (
              <p className="field-error">{fieldErrors.deadline}</p>
            ) : null}
          </div>
        )}
      </div>

      {/* The ADVANCED panel (spec 2026-10-23 §3.1): everything the owner
          path does not ask — working defaults ride in, the teacher
          opens it only to change them. */}
      <details className="form-advanced">
        <summary>高级（可选）：奖励、文件类型、校验规则等</summary>

        <div className="form-grid">
          <div className="field">
            <label className="field-label" htmlFor="task-reward">
              基础奖励积分
            </label>
            <input
              id="task-reward"
              className="input"
              inputMode="numeric"
              value={values.baseRewardPoints}
              onChange={(event) => setField("baseRewardPoints", event.target.value)}
              required
              aria-invalid={fieldErrors.baseRewardPoints !== undefined}
              disabled={lock("baseRewardPoints")}
            />
            {isFrozen("baseRewardPoints") ? (
              <p className="field-hint">{FROZEN_HINT}</p>
            ) : null}
            {fieldErrors.baseRewardPoints !== undefined ? (
              <p className="field-error">{fieldErrors.baseRewardPoints}</p>
            ) : null}
          </div>

          <div className="field">
            <label className="field-label" htmlFor="task-rarity">
              稀有度
            </label>
            <select
              id="task-rarity"
              className="input"
              value={values.rarity}
              onChange={(event) => setField("rarity", event.target.value)}
              disabled={lock("rarity")}
            >
              {TASK_RARITY_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            {isFrozen("rarity") ? <p className="field-hint">{FROZEN_HINT}</p> : null}
          </div>
        </div>

        <div className="form-grid">
          <div className="field">
            <label className="field-label" htmlFor="task-deadline-mode">
              截止模式
            </label>
            <select
              id="task-deadline-mode"
              className="input"
              value={values.deadlineMode}
              onChange={(event) =>
                setField(
                  "deadlineMode",
                  event.target.value === "RELATIVE" ? "RELATIVE" : "FIXED",
                )
              }
              disabled={lock("deadlineMode")}
            >
              {DEADLINE_MODE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            {isFrozen("deadlineMode") ? (
              <p className="field-hint">{FROZEN_HINT}</p>
            ) : null}
          </div>

          {values.deadlineMode === "RELATIVE" ? (
            <div className="field">
              <label className="field-label" htmlFor="task-duration">
                提交时限（分钟）
              </label>
              <input
                id="task-duration"
                className="input"
                inputMode="numeric"
                value={values.durationMinutes}
                onChange={(event) => setField("durationMinutes", event.target.value)}
                required
                disabled={lock("durationMinutes")}
              />
              {isFrozen("durationMinutes") ? (
                <p className="field-hint">{FROZEN_HINT}</p>
              ) : null}
            </div>
          ) : null}
        </div>

        <div className="form-grid">
          <div className="field">
            <label className="field-label" htmlFor="task-cutoff">
              领取截止（发布后分钟数）
            </label>
            <input
              id="task-cutoff"
              className="input"
              inputMode="numeric"
              value={values.claimCutoffMinutes}
              onChange={(event) => setField("claimCutoffMinutes", event.target.value)}
              aria-invalid={fieldErrors.claimCutoffMinutes !== undefined}
              disabled={lock("claimCutoffMinutes")}
            />
            {isFrozen("claimCutoffMinutes") ? (
              <p className="field-hint">{FROZEN_HINT}</p>
            ) : null}
            {fieldErrors.claimCutoffMinutes !== undefined ? (
              <p className="field-error">{fieldErrors.claimCutoffMinutes}</p>
            ) : null}
          </div>

          <div className="field">
            <label className="field-label" htmlFor="task-size">
              单文件上限（MB）
            </label>
            <input
              id="task-size"
              className="input"
              inputMode="decimal"
              value={values.maxFileSizeMb}
              onChange={(event) => setField("maxFileSizeMb", event.target.value)}
              aria-invalid={fieldErrors.maxFileSizeMb !== undefined}
              disabled={lock("maxFileSizeMb")}
            />
            {isFrozen("maxFileSizeMb") ? (
              <p className="field-hint">{FROZEN_HINT}</p>
            ) : null}
            {fieldErrors.maxFileSizeMb !== undefined ? (
              <p className="field-error">{fieldErrors.maxFileSizeMb}</p>
            ) : null}
          </div>
        </div>

        <fieldset className="field">
          <legend className="field-label">允许的文件类型</legend>
          <div className="perm-checks" role="group" aria-label="允许的文件类型">
            {ALLOWED_TASK_FILE_TYPES.map((type) => (
              <label key={type} className="identity-option">
                <input
                  type="checkbox"
                  checked={values.fileTypes.includes(type)}
                  onChange={() => {
                    const cast = type as TaskFormValues["fileTypes"][number];
                    setField(
                      "fileTypes",
                      values.fileTypes.includes(cast)
                        ? values.fileTypes.filter((item) => item !== cast)
                        : [...values.fileTypes, cast],
                    );
                  }}
                  disabled={lock("fileTypes")}
                />
                <span>{type}</span>
              </label>
            ))}
          </div>
          {fieldErrors.fileTypes !== undefined ? (
            <p className="field-error">{fieldErrors.fileTypes}</p>
          ) : null}
          <p className="field-hint">
            {isFrozen("fileTypes") ? `${FROZEN_HINT}。` : ""}
            平台上限 {MAX_TASK_FILE_SIZE_BYTES / (1024 * 1024)} MB，发布前至少配置一种类型。
          </p>
        </fieldset>

        {/* The schema is OPTIONAL (spec 2026-10-23 §2.1): empty = the
            lenient machine check; the template button offers a starting
            point for those who want the strict DSL. */}
        <div className="form-grid">
          <div className="field">
            <label className="field-label" htmlFor="task-schema">
              提交校验规则（JSON，可选）
            </label>
            <textarea
              id="task-schema"
              className="input mono"
              rows={3}
              placeholder='留空 = 宽松校验（表头+至少一行数据即通过）；严格校验可留空后点右侧按钮填入模板'
              value={values.submissionSchema}
              onChange={(event) => setField("submissionSchema", event.target.value)}
              aria-invalid={fieldErrors.submissionSchema !== undefined}
              disabled={lock("submissionSchema")}
            />
            <p className="field-hint">
              <button
                type="button"
                className="link"
                onClick={() =>
                  setField("submissionSchema", defaultSchemaFor(values.fileTypes))
                }
                disabled={lock("submissionSchema")}
              >
                使用模板
              </button>
              {" "}（按所选文件类型填入起点，请改成任务实际需要的列名）
            </p>
            {isFrozen("submissionSchema") ? (
              <p className="field-hint">{FROZEN_HINT}</p>
            ) : null}
            {fieldErrors.submissionSchema !== undefined ? (
              <p className="field-error">{fieldErrors.submissionSchema}</p>
            ) : null}
          </div>

          <div className="field">
            <label className="field-label" htmlFor="task-schema-version">
              校验规则版本
            </label>
            <input
              id="task-schema-version"
              className="input"
              inputMode="numeric"
              placeholder="1"
              value={values.submissionSchemaVersion}
              onChange={(event) =>
                setField("submissionSchemaVersion", event.target.value)
              }
              disabled={lock("submissionSchemaVersion")}
            />
            {isFrozen("submissionSchemaVersion") ? (
              <p className="field-hint">{FROZEN_HINT}</p>
            ) : null}
          </div>
        </div>

        {/* The notification policy (spec 2026-10-23 §3.2: 高级折叠保留
            通知通道; §3.1: 通知关闭 is the one-click default). Reminders
            need a channel to land on — the band lives in
            `validateTaskForm`. */}
        <fieldset className="field">
          <legend className="field-label">通知提醒</legend>
          <div className="perm-checks" role="group" aria-label="通知提醒">
            <label className="identity-option">
              <input
                type="checkbox"
                checked={values.notify24h}
                onChange={() => setField("notify24h", !values.notify24h)}
                disabled={lock("notify24h")}
              />
              <span>提前 24 小时提醒</span>
            </label>
            <label className="identity-option">
              <input
                type="checkbox"
                checked={values.notify4h}
                onChange={() => setField("notify4h", !values.notify4h)}
                disabled={lock("notify4h")}
              />
              <span>提前 4 小时提醒</span>
            </label>
          </div>
          <p className="field-hint">默认关闭；开启后按下方所选通道发送截止提醒。</p>
        </fieldset>

        <fieldset className="field">
          <legend className="field-label">通知通道</legend>
          <div className="perm-checks" role="group" aria-label="通知通道">
            {TASK_NOTIFICATION_CHANNEL_OPTIONS.map((option) => (
              <label key={option.value} className="identity-option">
                <input
                  type="checkbox"
                  checked={values.notificationChannels.includes(option.value)}
                  onChange={() =>
                    setField(
                      "notificationChannels",
                      values.notificationChannels.includes(option.value)
                        ? values.notificationChannels.filter(
                            (channel) => channel !== option.value,
                          )
                        : [...values.notificationChannels, option.value],
                    )
                  }
                  disabled={lock("notificationChannels")}
                />
                <span>{option.label}</span>
              </label>
            ))}
          </div>
          {fieldErrors.notificationChannels !== undefined ? (
            <p className="field-error">{fieldErrors.notificationChannels}</p>
          ) : null}
        </fieldset>
      </details>
    </>
  );
}
