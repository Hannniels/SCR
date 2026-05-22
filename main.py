from fastapi import FastAPI, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from paypalcheckoutsdk.core import PayPalHttpClient, SandboxEnvironment, LiveEnvironment
from paypalcheckoutsdk.orders import OrdersCreateRequest, OrdersCaptureRequest
import datetime
import uuid
import os

app = FastAPI()
engine = create_engine("sqlite:///./data.db")
Base = declarative_base()

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    current_price = Column(Float)
    daily_quota = Column(Integer)
    sold_today = Column(Integer, default=0)
    last_reset = Column(DateTime, default=datetime.datetime.utcnow)

class Order(Base):
    __tablename__ = "orders"
    id = Column(String, primary_key=True)
    product_id = Column(Integer)
    paypal_order_id = Column(String)
    status = Column(String)
    price_paid = Column(Float)

Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine)

PAYPAL_MODE = os.getenv("PAYPAL_MODE", "sandbox")
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.getenv("PAYPAL_SECRET", "")
PAYPAL_RECEIVER = os.getenv("PAYPAL_RECEIVER_EMAIL", "")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

if PAYPAL_MODE == "sandbox":
    env = SandboxEnvironment(client_id=PAYPAL_CLIENT_ID, client_secret=PAYPAL_SECRET)
else:
    env = LiveEnvironment(client_id=PAYPAL_CLIENT_ID, client_secret=PAYPAL_SECRET)
paypal_client = PayPalHttpClient(env)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def check_quota(db, product_id):
    product = db.query(Product).filter(Product.id == product_id).first()
    now = datetime.datetime.utcnow()
    if (now - product.last_reset).days >= 1:
        product.sold_today = 0
        product.last_reset = now
        db.commit()
    available = product.daily_quota - product.sold_today
    return available > 0, available

@app.get("/", response_class=HTMLResponse)
async def home(db: Session = Depends(get_db)):
    product = db.query(Product).first()
    if not product:
        return HTMLResponse("No product configured")
    ok, left = check_quota(db, product.id)
    html = f"""
    <html><head><title>{product.name}</title></head><body>
    <h1>{product.name}</h1>
    <p>Price: {product.current_price:.2f} USD</p >
    <p>Remaining today: {left}/{product.daily_quota}</p >
    """
    if ok:
        html += f'<a href=" "><button>Buy Now</button></a >'
    else:
        html += "<p style='color:red'>Sold out today, come back tomorrow</p >"
    html += "</body></html>"
    return html

@app.get("/buy/{product_id}")
async def buy(product_id: int, db: Session = Depends(get_db)):
    ok, _ = check_quota(db, product_id)
    if not ok:
        return HTMLResponse("No availability")
    product = db.query(Product).filter(Product.id == product_id).first()
    order_id = str(uuid.uuid4())
    request = OrdersCreateRequest()
    request.request_body({
        "intent": "CAPTURE",
        "purchase_units": [{
            "amount": {"currency_code": "USD", "value": f"{product.current_price:.2f}"},
            "payee": {"email_address": PAYPAL_RECEIVER}
        }],
        "application_context": {
            "return_url": f"{BASE_URL}/confirm?order_id={order_id}",
            "cancel_url": f"{BASE_URL}/cancel"
        }
    })
    response = await paypal_client.execute(request)
    paypal_order_id = response.result.id
    order = Order(id=order_id, product_id=product_id, paypal_order_id=paypal_order_id, status="pending", price_paid=product.current_price)
    db.add(order)
    db.commit()
    for link in response.result.links:
        if link.rel == "approve":
            return RedirectResponse(link.href)
    return HTMLResponse("PayPal error")

@app.get("/confirm")
async def confirm(order_id: str, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return HTMLResponse("Invalid order")
    capture = OrdersCaptureRequest(order.paypal_order_id)
    response = await paypal_client.execute(capture)
    if response.result.status == "COMPLETED":
        order.status = "completed"
        product = db.query(Product).filter(Product.id == order.product_id).first()
        product.sold_today += 1
        product.current_price += 5
        db.commit()
        return HTMLResponse(f"Payment successful! Your access code: BOT-{uuid.uuid4().hex[:8].upper()}")
    return HTMLResponse("Payment failed")

@app.get("/cancel")
async def cancel():
    return HTMLResponse("Payment cancelled")

@app.on_event("startup")
def init():
    db = SessionLocal()
    if not db.query(Product).first():
        p = Product(name="Crypto Hunter Bot", current_price=50.0, daily_quota=10)
        db.add(p)
        db.commit()
    db.close()
