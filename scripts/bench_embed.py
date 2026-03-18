"""Benchmark: measure actual embedding time per entity on CPU."""

import time
import statistics

# Simulate realistic code entity texts of varying sizes
SAMPLES = [
    # Tiny (getter)
    "def get_name(self): return self._name",
    # Small function
    "def validate_email(email: str) -> bool:\n    import re\n    pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\\.[a-zA-Z0-9-.]+$'\n    return bool(re.match(pattern, email))",
    # Medium function
    """def process_payment(self, order_id: str, amount: float, currency: str = 'USD') -> dict:
    if amount <= 0:
        raise ValueError("Amount must be positive")
    order = self.order_repo.get(order_id)
    if not order:
        raise OrderNotFoundError(order_id)
    payment = PaymentIntent(amount=amount, currency=currency, order_id=order_id)
    result = self.stripe_client.create_charge(payment)
    if result.status == 'succeeded':
        order.mark_paid(result.charge_id)
        self.order_repo.save(order)
        self.event_bus.publish(PaymentCompleted(order_id=order_id, charge_id=result.charge_id))
    return {'status': result.status, 'charge_id': result.charge_id}""",
    # Large function (will be chunked)
    """def reconcile_inventory(self, warehouse_id: str, date_range: tuple) -> ReconciliationReport:
    start_date, end_date = date_range
    warehouse = self.warehouse_repo.get(warehouse_id)
    if not warehouse:
        raise WarehouseNotFoundError(warehouse_id)
    
    # Fetch all transactions in the date range
    transactions = self.transaction_repo.find_by_warehouse_and_date(
        warehouse_id=warehouse_id, start=start_date, end=end_date
    )
    
    # Group by SKU
    sku_movements = {}
    for txn in transactions:
        if txn.sku not in sku_movements:
            sku_movements[txn.sku] = {'inbound': 0, 'outbound': 0, 'adjustments': 0}
        if txn.type == TransactionType.INBOUND:
            sku_movements[txn.sku]['inbound'] += txn.quantity
        elif txn.type == TransactionType.OUTBOUND:
            sku_movements[txn.sku]['outbound'] += txn.quantity
        else:
            sku_movements[txn.sku]['adjustments'] += txn.quantity
    
    # Compare with current stock levels
    discrepancies = []
    for sku, movements in sku_movements.items():
        current = self.stock_repo.get_level(warehouse_id, sku)
        expected = movements['inbound'] - movements['outbound'] + movements['adjustments']
        if abs(current.quantity - expected) > self.tolerance:
            discrepancies.append(Discrepancy(
                sku=sku, expected=expected, actual=current.quantity,
                delta=current.quantity - expected
            ))
    
    report = ReconciliationReport(
        warehouse_id=warehouse_id, period=(start_date, end_date),
        total_skus=len(sku_movements), discrepancies=discrepancies,
        status='clean' if not discrepancies else 'needs_review'
    )
    self.report_repo.save(report)
    self.notify_if_critical(report)
    return report""",
]

def main():
    print("Loading CodeBERT model...")
    t0 = time.time()
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("microsoft/codebert-base")
    load_time = time.time() - t0
    print(f"Model loaded in {load_time:.1f}s\n")

    # Warmup
    model.encode("warmup", show_progress_bar=False)

    # Single entity timing
    print("=== Single entity encode times ===")
    for i, text in enumerate(SAMPLES):
        times = []
        for _ in range(10):
            t = time.time()
            model.encode(text, show_progress_bar=False)
            times.append(time.time() - t)
        avg = statistics.mean(times) * 1000
        med = statistics.median(times) * 1000
        print(f"  Sample {i} ({len(text):>4} chars): avg={avg:.1f}ms  median={med:.1f}ms")

    # Batch timing (realistic: 256 entities)
    print("\n=== Batch encode (256 entities) ===")
    batch = SAMPLES * 64  # 256 items
    times = []
    for _ in range(3):
        t = time.time()
        model.encode(batch, show_progress_bar=False, batch_size=256)
        times.append(time.time() - t)
    avg = statistics.mean(times)
    per_entity = avg / 256 * 1000
    print(f"  Total: {avg:.2f}s  Per entity: {per_entity:.1f}ms")

    # Extrapolations
    print("\n=== Extrapolations (CPU only) ===")
    for count in [4000, 50000, 500000, 50_000_000]:
        total_sec = count * per_entity / 1000
        if total_sec < 60:
            print(f"  {count:>12,} entities: {total_sec:.0f}s")
        elif total_sec < 3600:
            print(f"  {count:>12,} entities: {total_sec/60:.1f} min")
        elif total_sec < 86400:
            print(f"  {count:>12,} entities: {total_sec/3600:.1f} hours")
        else:
            print(f"  {count:>12,} entities: {total_sec/86400:.1f} days")

if __name__ == "__main__":
    main()
