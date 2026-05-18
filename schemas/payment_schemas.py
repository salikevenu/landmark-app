from pydantic import BaseModel, validator
from typing import Optional

class CreateOrder(BaseModel):
    plan: str

    @validator('plan')
    def validate_plan(cls, v):
        allowed = ['service', 'basic', 'premium']
        if v not in allowed:
            raise ValueError(f'Plan must be one of {allowed}')
        return v

class VerifyPayment(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    plan: str

    @validator('plan')
    def validate_plan(cls, v):
        allowed = ['service', 'basic', 'premium']
        if v not in allowed:
            raise ValueError(f'Invalid plan')
        return v