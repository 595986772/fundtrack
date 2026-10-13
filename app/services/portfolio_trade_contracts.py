"""外部申赎登记的输入协议；这些接口不具备实际下单/撤单权限。"""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class TradeCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(default="guest", min_length=1, max_length=50)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,99}$")


class TradeOrderCreateRequest(TradeCommand):
    fund_code: str = Field(pattern=r"^\d{6}$")
    transaction_type: Literal["buy", "sell"]
    submitted_at: AwareDatetime
    requested_amount: Decimal | None = Field(default=None, gt=0, le=10**12)
    requested_units: Decimal | None = Field(default=None, gt=0, le=10**12)
    channel: Literal["off_exchange", "exchange"] = "off_exchange"
    note: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def check_direction(self):
        # 申请时还没有确认净值，买入填金额、卖出填份额，不能伪造预计成交。
        if self.transaction_type == "buy" and (self.requested_amount is None or self.requested_units is not None):
            raise ValueError("买入申请只填写申购金额")
        if self.transaction_type == "sell" and (self.requested_units is None or self.requested_amount is not None):
            raise ValueError("卖出申请只填写赎回份额")
        return self


class TradeOrderActionRequest(TradeCommand):
    action: Literal["cancel_request", "cancel_success", "cancel_rejected", "reject", "confirm", "settle"]
    reason: str = Field(min_length=1, max_length=300)
    trade_date: date | None = None
    confirmation_date: date | None = None
    settlement_date: date | None = None
    units: Decimal | None = Field(default=None, gt=0, le=10**12)
    unit_price: Decimal | None = Field(default=None, gt=0, le=10**12)
    fee: Decimal = Field(default=Decimal("0"), ge=0, le=10**12)

    @model_validator(mode="after")
    def check_action_fields(self):
        if self.action == "confirm":
            if any(value is None for value in (self.trade_date, self.confirmation_date, self.units, self.unit_price)):
                raise ValueError("确认成交需要净值日、确认日期、确认份额和成交净值")
            if self.settlement_date is not None:
                raise ValueError("到账须单独登记，不能由确认成交自动推断")
        elif self.action == "settle":
            if self.settlement_date is None:
                raise ValueError("请填写实际到账日期")
            if any(value is not None for value in (self.trade_date, self.confirmation_date, self.units, self.unit_price)) or self.fee:
                raise ValueError("到账操作不接受新的成交数据")
        elif any(value is not None for value in (self.trade_date, self.confirmation_date, self.settlement_date, self.units, self.unit_price)) or self.fee:
            raise ValueError("撤单或失败结果不接受成交数据")
        return self


class TransactionReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fund_code: str = Field(pattern=r"^\d{6}$")
    transaction_type: Literal["buy", "sell"]
    trade_date: date
    units: Decimal = Field(gt=0, le=10**12)
    unit_price: Decimal = Field(gt=0, le=10**12)
    fee: Decimal = Field(default=Decimal("0"), ge=0, le=10**12)
    note: str = Field(default="", max_length=200)


class TransactionCorrectionRequest(TradeCommand):
    reason: str = Field(min_length=1, max_length=300)
    # 不填 replacement 表示冲正错误录入，填写则在同一事务内追加正确流水。
    replacement: TransactionReplacement | None = None
