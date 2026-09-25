"""A small gateway service - fixture for project analysis."""
import logging
import requests
from fastapi import FastAPI

from helpers import charge_card, reserve_stock

log = logging.getLogger("orders")
app = FastAPI()

ORCH_URL = "http://localhost:6004"


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/orders")
def create_order(cart: dict):
    log.info("ORDER START order=%s items=%d", cart.get("id"), len(cart))
    stock = reserve_stock(cart)
    payment = charge_card(cart)
    try:
        reply = requests.post(f"{ORCH_URL}/notify", json=cart, timeout=5)
        log.info("notify sent status=%s", reply.status_code)
    except requests.RequestException:
        log.warning("notify failed - continuing without confirmation email")
    log.info("ORDER COMPLETED order=%s", cart.get("id"))
    return {"ok": True, "payment": payment, "stock": stock}


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    log.info("fetching order=%s", order_id)
    return lookup(order_id)


def lookup(order_id: str):
    import sqlite3
    conn = sqlite3.connect("orders.db")
    return conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
