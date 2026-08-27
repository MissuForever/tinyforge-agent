"""Create a reproducible buggy project for a short TinyForge demonstration."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


FILES = {
    "README.md": """# Order Total Demo

Implement `order_total(items)` in `pricing.py`.

Requirements:
- Each item has `price` and `quantity`; quantities must be included in the total.
- Money is rounded to two decimal places using normal financial ROUND_HALF_UP behavior.
- Empty orders return Decimal('0.00').
- Negative prices or quantities raise ValueError.
- Do not change the tests.
""",
    "pricing.py": """from decimal import Decimal


def order_total(items):
    # BUG: quantity, validation and currency rounding are missing.
    return sum(Decimal(str(item[\"price\"])) for item in items)
""",
    "tests/__init__.py": "",
    "tests/test_pricing.py": """import unittest
from decimal import Decimal

from pricing import order_total


class OrderTotalTests(unittest.TestCase):
    def test_uses_quantity(self):
        items = [
            {\"price\": \"12.50\", \"quantity\": 2},
            {\"price\": \"3.20\", \"quantity\": 1},
        ]
        self.assertEqual(order_total(items), Decimal(\"28.20\"))

    def test_rounds_half_up_to_cents(self):
        self.assertEqual(
            order_total([{\"price\": \"1.005\", \"quantity\": 1}]),
            Decimal(\"1.01\"),
        )

    def test_empty_order(self):
        self.assertEqual(order_total([]), Decimal(\"0.00\"))

    def test_rejects_negative_values(self):
        with self.assertRaises(ValueError):
            order_total([{\"price\": \"-1.00\", \"quantity\": 1}])
        with self.assertRaises(ValueError):
            order_total([{\"price\": \"1.00\", \"quantity\": -1}])


if __name__ == \"__main__\":
    unittest.main()
""",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=".demo/order_total")
    parser.add_argument("--force", action="store_true", help="Replace an existing demo directory")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    target = (project_root / args.target).resolve()
    if target == project_root or project_root not in target.parents:
        parser.error("target must be a subdirectory of the TinyForge project")
    if target.exists():
        if not args.force:
            parser.error(f"target already exists: {target}; use --force to reset it")
        shutil.rmtree(target)

    for relative_path, content in FILES.items():
        destination = target / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8", newline="")
    print(f"Created buggy demo project at {target}")
    print(
        "Run from the TinyForge project root: "
        f"py -3 -m unittest discover -s {target / 'tests'} -t {target} -v"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
