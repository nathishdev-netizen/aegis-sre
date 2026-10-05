# Example logs

## `shipyard.log`

A fictional order-fulfilment platform — five services, 26 orders, 175 lines.
Used for every example in [docs/AEGIS.md](../docs/AEGIS.md) so the whole
document can be reproduced without access to any real system.

```bash
python3 run.py          # then attach examples/shipyard.log in the UI
```

It contains, deliberately:

| | Count | Why it is there |
|---|---|---|
| Healthy orders | 20 | the baseline normal looks like |
| **Orders that never reserved stock** | 3 | the `hollow` case: paid, confirmed, nothing shipped |
| Declined payments | 2 | the `failed` case — the only one other tools catch |
| One slow catalogue lookup | 1 | the `degraded` case: 6s against a 1s p95, still HTTP 200 |

The three hollow orders are the point. They return `status=200`, charge the
card, and email the customer a confirmation. No error is logged because
nothing errored. Every dashboard renders them green, and the customer never
receives anything.

Services: `checkout-api`, `payment-svc`, `inventory-svc`, `notify-svc`,
`search-svc`. Correlation is on `order_id` — there is no trace id, which is
true of a great many real systems.
