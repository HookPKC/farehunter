#!/usr/bin/env python3
"""FareHunter v1 entry point.  Usage: python run.py [config.yaml] [prices.db]"""
import logging
import sys
sys.path.insert(0, "src")

from farehunter.runner import run, all_searches_failed

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    db = sys.argv[2] if len(sys.argv) > 2 else "prices.db"
    summary = run(config, db)
    print(f"完成: 搜尋 {summary['searched']} 次, 記錄 {summary['recorded']} 筆, "
          f"警報 {summary['alerts']} 則, 錯誤 {summary['errors']} 次, "
          f"空結果 {summary.get('empty', 0)} 次")
    # 零記錄航線與航線健康印在 stdout 而非只寫 log：job log 的 summary 行很容易
    # 被 200 行 INFO 淹掉，這兩行是「本輪有沒有航線悄悄斷線」的單一落點。
    if summary.get("zero_record_routes"):
        print(f"⚠ 本輪零記錄航線: {', '.join(summary['zero_record_routes'])}")
    _h = summary.get("health")
    if _h:
        print(f"航線健康: {_h['counts']}"
              + (f"  異常: {', '.join(_h['degraded'])}" if _h["degraded"] else ""))
    # 一次都沒拿到 API 回應（token 過期、Travelpayouts 全面故障）→ 讓這步變紅：
    # 後面的 export／commit／心跳都不跑，healthchecks 會通知。理由見
    # runner.all_searches_failed。
    if all_searches_failed(summary):
        print(f"✖ 本輪 {summary['searched']} 次搜尋全部失敗，以 exit 1 結束")
        sys.exit(1)
