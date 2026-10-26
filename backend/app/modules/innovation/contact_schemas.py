"""Shared contact-channel input contract for the innovation module.

Equivalence note (batch 1): extracted verbatim from the upstream
branch's ``recruitment_schemas.py`` so the talent profile can carry
chosen contact values without importing the recruitment DTO chain.
The validation delegates to the identity module's normalization (email
and phone) exactly like the upstream source; recruitment reuses this
file when batch 4 lands.
"""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from app.core.config import get_settings
from app.core.errors import BusinessError
from app.modules.identity.email_verification import EmailVerificationService
from app.modules.identity.otp import InvalidPhoneError, normalize_phone

ContactChannel = Literal["WECHAT", "QQ", "EMAIL", "PHONE"]


class ContactInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: ContactChannel
    value: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=254)
    ]

    @model_validator(mode="after")
    def normalize(self) -> "ContactInput":
        if self.channel == "EMAIL":
            try:
                self.value = EmailVerificationService._normalized_email(self.value)
            except BusinessError as exc:
                raise ValueError("请输入有效邮箱") from exc
            if not re.fullmatch(
                r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?",
                self.value,
            ):
                raise ValueError("请输入有效邮箱")
        elif self.channel == "PHONE":
            try:
                self.value = normalize_phone(
                    self.value, get_settings().phone_default_region
                )
            except InvalidPhoneError as exc:
                raise ValueError("请输入有效手机号") from exc
        elif self.channel == "QQ":
            if not re.fullmatch(r"[1-9][0-9]{4,19}", self.value):
                raise ValueError("QQ 须为 5～20 位数字")
        elif not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{5,19}", self.value):
            raise ValueError("微信号须为 6～20 位字母、数字、下划线或连字符")
        return self
