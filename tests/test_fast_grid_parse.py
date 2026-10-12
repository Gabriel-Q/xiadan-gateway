# -*- coding: utf-8 -*-
"""极速网格 TSV 解析测试：OCR 全空/零噪声行丢弃、真实行保留

背景（2026-10-12 压测实锤）：F4 当日委托页 12 行中 3 行为整行空/零
（OCR 网格空白行），污染 count 与逐单核对；真实数据行必有代码/名称/
时间/合同编号等非零内容（判据见 FastTrader._is_noise_row）。
"""
from src.services.fast_trader import FastTrader

HEADER = "证券代码\t证券名称\t委托价格\t委托数量\t合同编号"


def _tsv(*lines: str) -> str:
    return "\n".join((HEADER,) + lines)


class TestParseTsvNoiseRows:
    def test_only_header_is_empty_list(self):
        header, rows = FastTrader._parse_tsv(HEADER)
        assert header and rows == []

    def test_real_rows_kept(self):
        data = _tsv(
            "000001\t平安银行\t10.600\t100\t6295973145",
            "000001\t平安银行\t12.850\t100\t6295970697",
        )
        _, rows = FastTrader._parse_tsv(data)
        assert len(rows) == 2
        assert rows[0]["合同编号"] == "6295973145"

    def test_zero_noise_row_dropped(self):
        """压测实测噪声行形态：全空串 + 零值占位"""
        data = _tsv(
            "000001\t平安银行\t10.600\t100\t6295973145",
            "\t\t0.000\t0\t",
            "000001\t平安银行\t12.850\t100\t6295970697",
        )
        _, rows = FastTrader._parse_tsv(data)
        assert len(rows) == 2
        assert all(r["证券代码"] for r in rows)

    def test_dash_placeholder_noise_row_dropped(self):
        """横杠占位（部分网格渲染空值的形式）同样按噪声处理"""
        data = _tsv(
            "000001\t平安银行\t10.600\t100\t6295973145",
            "-\t--\t-\t--\t-",
        )
        _, rows = FastTrader._parse_tsv(data)
        assert len(rows) == 1

    def test_zero_price_real_row_kept(self):
        """真实行有非零文本锚（代码/合同编号），零价格不误杀"""
        data = _tsv("000001\t平安银行\t0.000\t0\t6295973145")
        _, rows = FastTrader._parse_tsv(data)
        assert len(rows) == 1

    def test_short_line_padded_with_empty(self):
        """单元格数少于表头 → 缺失列补空串（原行为保留）"""
        data = _tsv("000001\t平安银行")
        _, rows = FastTrader._parse_tsv(data)
        assert len(rows) == 1
        assert rows[0]["合同编号"] == ""

    def test_empty_input(self):
        assert FastTrader._parse_tsv("") == ([], [])
