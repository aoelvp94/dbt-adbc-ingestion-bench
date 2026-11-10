"""The dataset must be deterministic: same seed -> byte-identical files."""

import json

from bench import generate


def test_generator_is_deterministic(tmp_path, monkeypatch):
    hashes = []
    for i in range(2):
        monkeypatch.setattr(generate, "DATA_DIR", tmp_path / str(i))
        out = generate.generate("1k", force=True)
        hashes.append(json.loads((out / "manifest.json").read_text())["tables"])
    assert hashes[0] == hashes[1]


def test_shapes_and_nulls():
    import numpy as np

    rng = np.random.default_rng(generate.SEED)
    customers = generate.customers(50, rng)
    orders = generate.orders(1000, 50, rng)
    assert customers.num_rows == 50 and orders.num_rows == 1000
    assert orders.column("customer_id").to_numpy().max() <= 50
    nulls = orders.column("coupon_code").null_count
    assert 100 < nulls < 1000  # ~80% of orders have no coupon
