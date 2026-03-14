"""models.py — Request and response models."""
 
from typing import Optional, Literal
from pydantic import BaseModel
 
class OrderIntent(BaseModel):
    symbol:          str
    side:            Literal["buy", "sell"]
    order_type:      Literal["MKT", "LMT"] = "MKT"
    tif:             Literal["DAY", "GTC"] = "DAY"
    quantity:        Optional[float] = None
    limit_price:     Optional[float] = None
    client_order_id: Optional[str]   = None
 
class PlaceRequest(BaseModel):
    orders: list[OrderIntent]
 
class PlaceResponse(BaseModel):
    ok:            bool
    timestamp_utc: str
    placed:        list[dict]
    warnings:      list[str] = []
 
class CancelRequest(BaseModel):
    order_id: int
 
class TradingHaltRequest(BaseModel):
    reason: Optional[str] = None
 