import logging
import requests

log = logging.getLogger("orders")
PAYMENT_URL = "https://payments.example.com/charge"


def reserve_stock(cart):
    log.info("reserving stock for order=%s", cart.get("id"))
    # no try/except, no timeout - deliberately unguarded
    reply = requests.post("http://localhost:7002/reserve", json=cart)
    return reply.json()


def charge_card(cart):
    log.info("charging card for order=%s", cart.get("id"))
    try:
        reply = requests.post(PAYMENT_URL, json=cart, timeout=3)
        return reply.json()
    except requests.Timeout:
        log.error("payment timed out for order=%s", cart.get("id"))
        raise
