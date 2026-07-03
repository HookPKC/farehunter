#!/usr/bin/env python3
"""FareHunter v1 entry point.  Usage: python run.py [config.yaml] [prices.db]"""
import logging
import sys
sys.path.insert(0, "src")

from farehunter.runner import run

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    db = sys.argv[2] if len(sys.argv) > 2 else "prices.db"
    summary = run(config, db)
    print(f"完成: 搜尋 {summary['searched']} 次, 記錄 {summary['recorded']} 筆, "
          f"警報 {summary['alerts']} 則, 錯誤 {summary['errors']} 次")
