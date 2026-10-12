"use client";
import { AuthField } from "@/features/auth/AuthField";
import { codePointCount } from "./draftForm";
import { chooseRecruitmentStage, type RecruitmentForm, type RecruitmentFormErrors } from "./recruitmentForm";

export function RecruitmentStageField({ form, errors, onChange }: { form: RecruitmentForm; errors: RecruitmentFormErrors; onChange: (form: RecruitmentForm) => void }) {
  const error = errors.idea_stage || (form.idea_stage !== false ? errors.stage : undefined);
  const choice = form.idea_stage === true && !form.stage.trim() ? "" : String(form.idea_stage);
  return <div className="field">
    <label className="field-label" htmlFor="recruit-idea_stage">项目阶段</label>
    <select className="input" id="recruit-idea_stage" name="idea_stage" value={choice} aria-invalid={error ? true : undefined} aria-describedby={`recruit-stage-choice-hint${error ? " recruit-stage-choice-error" : ""}`} onChange={(event) => onChange(chooseRecruitmentStage(form, event.target.value))}>
      <option value="">请选择项目阶段</option><option value="true">创意起步</option><option value="false">其他阶段</option>
    </select>
    <p className="field-hint" id="recruit-stage-choice-hint">创意起步时须填写阶段目标与初步方案；其他阶段按实际情况填写。</p>
    {error ? <p className="field-error" id="recruit-stage-choice-error">{error}</p> : null}
    {form.idea_stage === false ? <AuthField name="stage" label="具体阶段" counter={`${codePointCount(form.stage.trim())}/80`} error={errors.stage} hint="例如调研中、原型验证、运营中。发布时必填，最多 80 个字符。" inputProps={{ type: "text", value: form.stage, onChange: (event) => onChange({ ...form, stage: event.target.value }) }} /> : null}
    {form.idea_stage === true && form.stage && form.stage !== "创意起步" ? <p className="field-hint">已保存的阶段说明：{form.stage}</p> : null}
  </div>;
}
