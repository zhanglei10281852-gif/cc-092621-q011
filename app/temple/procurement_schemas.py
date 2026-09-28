from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class PackageMaterialInput(BaseModel):
    material_code: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")
    name: str = Field(min_length=1, max_length=160)
    unit: str = Field(min_length=1, max_length=16)
    spec: str = Field(default="", max_length=300)
    quantity: float = Field(ge=0, le=1_000_000_000)
    unit_price: float = Field(default=0, ge=0, le=1_000_000_000)


class ProcurementPackageCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    restoration_campaign_code: str | None = Field(default=None, max_length=80)
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=160)
    currency: str = Field(default="CNY", min_length=2, max_length=8)
    budget_limit: float = Field(ge=0, le=1_000_000_000_000)
    required_signatures: list[str] = Field(default_factory=list, max_length=20)
    materials: list[PackageMaterialInput] = Field(default_factory=list, max_length=1000)
    actor: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_materials(self) -> "ProcurementPackageCreate":
        codes = [item.material_code for item in self.materials]
        if len(codes) != len(set(codes)):
            raise ValueError("采购清单中的材料编码不能重复")
        return self


class DraftLineUpsert(BaseModel):
    material_code: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")
    name: str = Field(min_length=1, max_length=160)
    unit: str = Field(min_length=1, max_length=16)
    spec: str = Field(default="", max_length=300)
    quantity: float = Field(ge=0, le=1_000_000_000)
    unit_price: float = Field(default=0, ge=0, le=1_000_000_000)
    actor: str = Field(min_length=1, max_length=120)


class DraftLineRemove(BaseModel):
    actor: str = Field(min_length=1, max_length=120)


class PackageAction(BaseModel):
    actor: str = Field(min_length=1, max_length=120)


class ReceiptCreate(BaseModel):
    material_code: str = Field(min_length=2, max_length=64)
    quantity: float = Field(gt=0, le=1_000_000_000)
    unit_price: float | None = Field(default=None, ge=0, le=1_000_000_000)
    reference: str = Field(default="", max_length=120)
    actor: str = Field(min_length=1, max_length=120)


class ChangeOrderItemInput(BaseModel):
    change_kind: Literal["addition", "removal", "substitution", "adjustment"]
    material_code: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")
    name: str | None = Field(default=None, max_length=160)
    unit: str | None = Field(default=None, max_length=16)
    spec: str | None = Field(default=None, max_length=300)
    quantity: float | None = Field(default=None, ge=0, le=1_000_000_000)
    unit_price: float | None = Field(default=None, ge=0, le=1_000_000_000)
    quantity_delta: float | None = Field(default=None, ge=-1_000_000_000, le=1_000_000_000)
    new_unit_price: float | None = Field(default=None, ge=0, le=1_000_000_000)
    target_material_code: str | None = Field(default=None, max_length=64)
    target_name: str | None = Field(default=None, max_length=160)
    target_unit: str | None = Field(default=None, max_length=16)
    target_spec: str | None = Field(default=None, max_length=300)
    target_quantity: float | None = Field(default=None, ge=0, le=1_000_000_000)
    target_unit_price: float | None = Field(default=None, ge=0, le=1_000_000_000)
    reason: str = Field(default="", max_length=500)


class ChangeOrderCreate(BaseModel):
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    title: str = Field(min_length=2, max_length=160)
    reason: str = Field(default="", max_length=500)
    impact: str = Field(default="", max_length=500)
    items: list[ChangeOrderItemInput] = Field(min_length=1, max_length=500)
    actor: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_items(self) -> "ChangeOrderCreate":
        codes = [item.material_code for item in self.items]
        if len(codes) != len(set(codes)):
            raise ValueError("变更单中同一材料不能出现多次")
        for item in self.items:
            if item.change_kind == "substitution" and not item.target_material_code:
                raise ValueError("替代变更必须提供替代材料编码")
        return self


class ChangeOrderSignature(BaseModel):
    signer_role: str = Field(min_length=2, max_length=64)
    signer: str = Field(min_length=1, max_length=120)


class ChangeOrderSubmit(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    committed_fund_amount: float = Field(default=0, ge=0, le=1_000_000_000_000)
    fund_reference: str = Field(default="", max_length=120)


class ChangeOrderDecision(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    note: str = Field(min_length=2, max_length=500)


class ChangeOrderResubmit(BaseModel):
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    title: str | None = Field(default=None, max_length=160)
    reason: str | None = Field(default=None, max_length=500)
    impact: str | None = Field(default=None, max_length=500)
    items: list[ChangeOrderItemInput] | None = Field(default=None, max_length=500)
    committed_fund_amount: float | None = Field(default=None, ge=0, le=1_000_000_000_000)
    fund_reference: str | None = Field(default=None, max_length=120)
    actor: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_items(self) -> "ChangeOrderResubmit":
        if self.items is not None:
            codes = [item.material_code for item in self.items]
            if len(codes) != len(set(codes)):
                raise ValueError("变更单中同一材料不能出现多次")
        return self
