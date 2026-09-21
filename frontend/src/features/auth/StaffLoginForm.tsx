"use client";
/**
 * Staff login island (spec §5.8; patterns §6) — a DEDICATED form on its own
 * route, never merged with the student number+password parser: the two
 * identifier classes stay unambiguous by construction (task brief step 3).
 *
 * Submit flow: email + password + second factor -> POST /api/v1/auth/staff/
 * login -> §29 envelope branching:
 * - success: refresh the session cache and land on home;
 * - `AUTHENTICATION_REQUIRED`: the uniform wrong email/password/code copy
 *   (one staff-specific line — never the student form's 学号 copy);
 * - `TOTP_SETUP_REQUIRED`: raised only AFTER the password proved correct
 *   (staff_service), i.e. the account exists but 2FA onboarding was
 *   abandoned midway. The 403 body carries NO session tokens, so the
 *   client cannot resume /staff/totp/* from here — and by the privacy
 *   rules of this feature the pending access token only ever lives in the
 *   invitation flow's React state, never in storage. The actionable panel
 *   below is therefore the honest deep-link: it points the user back to
 *   the setup ENTRY (the invitation link), which is the only sanctioned
 *   way to obtain a pending staff session.
 */
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { useSession } from "@/features/auth/session";
import { isApiError } from "@/lib/errors";

import { AuthField } from "./AuthField";
import { FormErrorSummary } from "./FormErrorSummary";
import { SubmitButton } from "./SubmitButton";
import { loginStaff } from "./api";
import { NETWORK_ERROR_TEXT, withFieldErrorSummary, type AuthErrorView } from "./errors";
import { describeStaffError, validateSecondFactor, validateStaffEmail } from "./staffAuthView";
import { validatePassword } from "./validation";

type StaffLoginFields = "email" | "password" | "totp_code";

export function StaffLoginForm() {
  const router = useRouter();
  const { refresh: refreshSession } = useSession();
  const [values, setValues] = useState({ email: "", password: "", totp_code: "" });
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<StaffLoginFields, string>>>({});
  const [summary, setSummary] = useState<AuthErrorView | null>(null);
  const [setupRequired, setSetupRequired] = useState(false);
  const [loading, setLoading] = useState(false);

  function setField(name: StaffLoginFields, value: string) {
    setValues((previous) => ({ ...previous, [name]: value }));
    setFieldErrors((previous) => {
      if (previous[name] === undefined) {
        return previous;
      }
      const rest: Partial<Record<StaffLoginFields, string>> = { ...previous };
      delete rest[name];
      return rest;
    });
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const email = values.email.trim(); // the backend lowercases/strips (§5.8)
    const errors: Partial<Record<StaffLoginFields, string>> = {
      email: validateStaffEmail(email) ?? undefined,
      password: validatePassword(values.password) ?? undefined,
      totp_code: validateSecondFactor(values.totp_code) ?? undefined,
    };
    setFieldErrors(errors);
    setSummary(null);
    setSetupRequired(false);
    if (Object.values(errors).some((message) => message !== undefined)) {
      return;
    }

    setLoading(true);
    try {
      await loginStaff(email, values.password, values.totp_code.trim());
      refreshSession();
      router.replace("/");
      router.refresh();
    } catch (error) {
      if (!isApiError(error)) {
        setSummary({ summary: NETWORK_ERROR_TEXT, fieldErrors: {}, requestId: null });
        return;
      }
      if (error.code === "TOTP_SETUP_REQUIRED") {
        setSetupRequired(true);
      }
      const view = withFieldErrorSummary(describeStaffError(error, "staff-login"));
      setSummary(view);
      setFieldErrors(view.fieldErrors as Partial<Record<StaffLoginFields, string>>);
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      {setupRequired ? (
        <div className="alert alert-warning" role="alert">
          <p>
            密码正确，但该账号尚未完成动态口令绑定，暂时无法登录。
            请打开邀请邮件中的链接继续完成绑定；若邀请链接已失效，请联系管理员重新发送邀请。
          </p>
        </div>
      ) : null}
      <form className="form" onSubmit={onSubmit} noValidate>
        <FormErrorSummary view={summary} />
        <AuthField
          name="email"
          label="邮箱"
          error={fieldErrors.email}
          inputProps={{
            value: values.email,
            onChange: (event) => setField("email", event.target.value),
            type: "email",
            inputMode: "email",
            autoComplete: "email",
            autoCapitalize: "none",
            autoCorrect: "off",
            placeholder: "员工邮箱",
            disabled: loading,
          }}
        />
        <AuthField
          name="password"
          label="密码"
          error={fieldErrors.password}
          inputProps={{
            value: values.password,
            onChange: (event) => setField("password", event.target.value),
            type: "password",
            autoComplete: "current-password",
            placeholder: "请输入密码",
            disabled: loading,
          }}
        />
        <AuthField
          name="totp_code"
          label="动态验证码"
          hint="验证器应用当前显示的 6 位数字；验证器不可用时输入一个恢复代码。"
          error={fieldErrors.totp_code}
          inputProps={{
            value: values.totp_code,
            onChange: (event) => setField("totp_code", event.target.value),
            type: "text",
            // No numeric inputMode: a recovery code carries hex + a dash.
            autoComplete: "one-time-code",
            autoCapitalize: "none",
            autoCorrect: "off",
            spellCheck: false,
            placeholder: "6 位数字或恢复代码",
            maxLength: 11,
            disabled: loading,
          }}
        />
        <SubmitButton loading={loading}>登录</SubmitButton>
      </form>
    </>
  );
}
