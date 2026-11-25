"""XAUUSD M30 CSV 数据源：读仓库根目录 XAUUSD_30m.csv，时间用 auto=False。

chan.py 内置 CSV_API 的 parse_time_column 用 CTime 默认 auto=True，
会把 00:00 的K线当成当天 23:59，导致 00:30 的K线报 kline time err。
本类按 quick_guide 建议把 auto 设为 False。
"""
import os

from Common.CEnum import DATA_FIELD, KL_TYPE
from Common.CTime import CTime
from KLine.KLine_Unit import CKLine_Unit

from .CommonStockAPI import CCommonStockApi


class XauCsv(CCommonStockApi):
    def __init__(self, code, k_type=KL_TYPE.K_30M, begin_date=None, end_date=None, autype=None):
        super(XauCsv, self).__init__(code, k_type, begin_date, end_date, autype)

    def get_kl_data(self):
        cur_path = os.path.dirname(os.path.realpath(__file__))
        file_path = os.path.join(cur_path, "..", "XAUUSD_30m.csv")
        with open(file_path, "r") as f:
            header = f.readline()
            for line in f:
                parts = line.strip("\n").split(",")
                if len(parts) != 5:
                    continue
                t = parts[0]  # YYYY-MM-DD HH:MM:SS
                item = {
                    DATA_FIELD.FIELD_TIME: CTime(
                        int(t[0:4]), int(t[5:7]), int(t[8:10]),
                        int(t[11:13]), int(t[14:16]), auto=False),
                    DATA_FIELD.FIELD_OPEN: float(parts[1]),
                    DATA_FIELD.FIELD_HIGH: float(parts[2]),
                    DATA_FIELD.FIELD_LOW: float(parts[3]),
                    DATA_FIELD.FIELD_CLOSE: float(parts[4]),
                }
                yield CKLine_Unit(item)

    def SetBasciInfo(self):
        pass

    @classmethod
    def do_init(cls):
        pass

    @classmethod
    def do_close(cls):
        pass
