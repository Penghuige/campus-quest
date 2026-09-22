"use client";
/**
 * The shared task form fields (create + edit, spec §6/§6.2; patterns §6).
 *
 * Extracted from the create dialog so the EDIT surface renders the same
 * field set from the same band logic. Layout only: values/errors/
 * disabled state arrive by props; submission and error mapping stay the
 * dialogs' (the pure band rules live in `validateTaskForm`).
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
}

const FROZEN_HINT = "已发布后不可修改";

export function TaskFormFields({
  values,
  fieldErrors,
  disabled,
  frozen,
  setField,
}: TaskFormFieldsProps) {
  const isFrozen = (key: TaskFormFieldKey): boolean => frozen?.has(key) ?? false;
  const lock = (key: TaskFormFieldKey): boolean => disabled || isFrozen(key);

  function toggleFileType(type: string) {
    const cast = type as TaskFormValues["fileTypes"][number];
    setField(
      "fileTypes",
      values.fileTypes.includes(cast)
        ? values.fileTypes.filter((item) => item !== cast)
        : [...values.fileTypes, cast],
    );
  }

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

        {values.deadlineMode === "FIXED" ? (
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
            {isFrozen("fixedDeadlineLocal") ? (
              <p className="field-hint">{FROZEN_HINT}</p>
            ) : null}
          </div>
        ) : (
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
              aria-invalid={fieldErrors.deadline !== undefined}
              disabled={lock("durationMinutes")}
            />
            {isFrozen("durationMinutes") ? (
              <p className="field-hint">{FROZEN_HINT}</p>
            ) : null}
          </div>
        )}
      </div>
      {fieldErrors.deadline !== undefined ? (
        <p className="field-error">{fieldErrors.deadline}</p>
      ) : null}

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
                onChange={() => toggleFileType(type)}
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

      <div className="form-grid">
        <div className="field">
          <label className="field-label" htmlFor="task-schema">
            提交校验 schema（JSON，发布前必填）
          </label>
          <textarea
            id="task-schema"
            className="input mono"
            rows={3}
            placeholder='{"columns": ["platform", "keyword"]}'
            value={values.submissionSchema}
            onChange={(event) => setField("submissionSchema", event.target.value)}
            aria-invalid={fieldErrors.submissionSchema !== undefined}
            disabled={lock("submissionSchema")}
          />
          {isFrozen("submissionSchema") ? (
            <p className="field-hint">{FROZEN_HINT}</p>
          ) : null}
          {fieldErrors.submissionSchema !== undefined ? (
            <p className="field-error">{fieldErrors.submissionSchema}</p>
          ) : null}
        </div>

        <div className="field">
          <label className="field-label" htmlFor="task-schema-version">
            schema 版本
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
    </>
  );
}
