from pydantic import BaseModel, validator, Field
from typing import Optional
import re

class PhoneOnly(BaseModel):
    phone: str = Field(..., min_length=10, max_length=10)

    @validator('phone')
    def validate_phone(cls, v):
        if not re.match(r'^[6-9]\d{9}$', v):
            raise ValueError('Invalid Indian phone number')
        return v

class RegisterUser(BaseModel):
    phone: str
    name: str
    referral_code: Optional[str] = None

    @validator('phone')
    def validate_phone(cls, v):
        if not re.match(r'^[6-9]\d{9}$', v):
            raise ValueError('Invalid Indian phone number')
        return v

class UpdateProfile(BaseModel):
    name: Optional[str] = None

class UpgradePlan(BaseModel):
    plan: str

    @validator('plan')
    def validate_plan(cls, v):
        allowed = ['service', 'basic', 'premium']
        if v not in allowed:
            raise ValueError(f'Plan must be one of {allowed}')
        return v

class VerifyOTP(BaseModel):
    phone: str
    otp: str = Field(..., min_length=6, max_length=6)

    @validator('phone')
    def validate_phone(cls, v):
        if not re.match(r'^[6-9]\d{9}$', v):
            raise ValueError('Invalid Indian phone number')
        return v