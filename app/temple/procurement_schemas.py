from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class SpecialFundCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    code: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=160)
    total_amount: float = Field(ge=0, le=10_000_000_000)
    note: str = Field(default="", max_length=500)
    actor: str = Field(min_length=1, max_length=120)


class MaterialLineInput(BaseModel):
    material_code: str = Field(min_length=1, max_length=80)
    material_name: str = Field(min_length=1, max_length=160)
    material_spec: str = Field(default="", max_length=300)
    material_unit: str = Field(default="", max_length=20)
    quantity: float = Field(gt=0, le=1_000_000_000)
    unit_price: float = Field(ge=0, le=1_000_000_000)


class ProcurementPackageCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    restoration_campaign_id: int | None = Field(default=None, gt=0)
    fund_code: str = Field(min_length=2, max_length=80)
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=160)
    scope_summary: str = Field(default="", max_length=1000)
    budget_cap: float = Field(ge=0, le=10_000_000_000)
    required_signoffs: list[str] = Field(default_factory=list, max_length=20)
    materials: list[MaterialLineInput] = Field(min_length=1, max_length=1000)
    actor: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_unique(self) -> "ProcurementPackageCreate":
        codes = [item.material_code for item in self.materials]
        if len(codes) != len(set(codes)):
            raise ValueError("清单材料编码不能重复")
        roles = [role for role in self.required_signoffs if role]
        if len(roles) != len(set(roles)):
            raise ValueError("必需签署角色不能重复")
        return self


class SignoffInput(BaseModel):
    signer_role: str = Field(min_length=1, max_length=80)
    signer: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=500)


class PackageFreeze(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    signoffs: list[SignoffInput] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_unique_roles(self) -> "PackageFreeze":
        roles = [item.signer_role for item in self.signoffs]
        if len(roles) != len(set(roles)):
            raise ValueError("同一采购包的签署角色不能重复")
        return self


class ChangeItemInput(BaseModel):
    kind: Literal["add", "increase", "decrease", "substitute"]
    material_code: str = Field(min_length=1, max_length=80)
    material_name: str = Field(default="", max_length=160)
    material_spec: str = Field(default="", max_length=300)
    material_unit: str = Field(default="", max_length=20)
    material_unit_price: float = Field(default=0, ge=0, le=1_000_000_000)
    quantity: float = Field(gt=0, le=1_000_000_000)
    substitute_code: str | None = Field(default=None, max_length=80)
    substitute_name: str | None = Field(default=None, max_length=160)
    substitute_spec: str | None = Field(default=None, max_length=300)
    substitute_unit: str | None = Field(default=None, max_length=20)
    substitute_unit_price: float | None = Field(default=None, ge=0, le=1_000_000_000)
    reason: str = Field(min_length=2, max_length=500)
    impact: str = Field(min_length=2, max_length=500)

    @model_validator(mode="after")
    def validate_shape(self) -> "ChangeItemInput":
        if self.kind == "substitute":
            if not self.substitute_code:
                raise ValueError("替代材料必须提供 substitute_code")
            if self.substitute_unit_price is None:
                raise ValueError("替代材料必须提供 substitute_unit_price")
        elif self.substitute_code or self.substitute_unit_price is not None:
            raise ValueError("只有替代项才能填写替代材料信息")
        return self


class ChangeOrderCreate(BaseModel):
    package_code: str = Field(min_length=3, max_length=80)
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    title: str = Field(min_length=2, max_length=160)
    items: list[ChangeItemInput] = Field(min_length=1, max_length=1000)
    actor: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_unique(self) -> "ChangeOrderCreate":
        keys = [(item.kind, item.material_code) for item in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("变更单内同一材料的同类变更不能重复")
        return self


class ChangeOrderSubmit(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    signoffs: list[SignoffInput] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_unique_roles(self) -> "ChangeOrderSubmit":
        roles = [item.signer_role for item in self.signoffs]
        if len(roles) != len(set(roles)):
            raise ValueError("同一变更单的签署角色不能重复")
        return self


class ChangeOrderDecision(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=500)


class ChangeOrderAction(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=500)


class ChangeOrderResubmit(BaseModel):
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    title: str | None = Field(default=None, min_length=2, max_length=160)
    items: list[ChangeItemInput] | None = Field(default=None, max_length=1000)
    actor: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_unique(self) -> "ChangeOrderResubmit":
        if self.items is not None:
            keys = [(item.kind, item.material_code) for item in self.items]
            if len(keys) != len(set(keys)):
                raise ValueError("变更单内同一材料的同类变更不能重复")
        return self


class MaterialReceiptInput(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    quantity: float = Field(gt=0, le=1_000_000_000)
    note: str = Field(default="", max_length=500)
