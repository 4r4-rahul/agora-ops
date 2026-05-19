"""Unit tests for agora/ops/payload_compressor.py"""
import json
import pytest
from agora.ops.payload_compressor import compress_payload, compress_text, _is_ohlcv_like


class TestCompressPayload:
    def test_strips_none_values(self):
        payload = {"a": 1, "b": None, "c": {"d": None, "e": 2}}
        out = json.loads(compress_payload(payload))
        assert "b" not in out
        assert "d" not in out["c"]
        assert out["c"]["e"] == 2

    def test_rounds_floats_to_2dp(self):
        payload = {"price": 189.123456, "ratio": 0.333333}
        out = json.loads(compress_payload(payload))
        assert out["price"] == 189.12
        assert out["ratio"] == 0.33

    def test_preserves_ints_and_strings(self):
        payload = {"count": 5, "ticker": "AAPL", "flag": True}
        out = json.loads(compress_payload(payload))
        assert out["count"] == 5
        assert out["ticker"] == "AAPL"
        assert out["flag"] is True

    def test_ohlcv_list_becomes_csv_string(self):
        payload = {"ohlcv": [
            {"date": "2026-01-01", "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0, "volume": 1000000},
            {"date": "2026-01-02", "open": 103.0, "high": 107.0, "low": 102.0, "close": 106.0, "volume": 1100000},
            {"date": "2026-01-03", "open": 106.0, "high": 108.0, "low": 104.0, "close": 107.0, "volume": 950000},
        ]}
        out = json.loads(compress_payload(payload))
        assert isinstance(out["ohlcv"], str)
        assert out["ohlcv"].startswith("csv:")
        assert "date,open,high,low,close,volume" in out["ohlcv"]

    def test_short_list_not_converted(self):
        payload = {"items": [{"date": "2026-01-01", "close": 100.0}]}
        out = json.loads(compress_payload(payload))
        assert isinstance(out["items"], list)

    def test_non_ohlcv_list_stays_list(self):
        payload = {"lessons": ["be careful", "check IV", "watch GEX"]}
        out = json.loads(compress_payload(payload))
        assert out["lessons"] == ["be careful", "check IV", "watch GEX"]

    def test_truncates_long_strings(self):
        payload = {"text": "x" * 7000}
        out = json.loads(compress_payload(payload))
        assert len(out["text"]) <= 6002  # 6000 + ellipsis

    def test_produces_smaller_output_than_raw_json(self):
        payload = {
            "ticker": "AAPL",
            "price": 189.123456,
            "iv_rank": 42.333333,
            "macro": None,
            "ohlcv": [
                {"date": f"2026-01-{i:02d}", "open": 100.0+i, "high": 105.0+i,
                 "low": 99.0+i, "close": 103.0+i, "volume": 1000000+i}
                for i in range(1, 8)
            ],
        }
        raw = json.dumps(payload, default=str)
        compressed = compress_payload(payload)
        assert len(compressed) < len(raw)

    def test_nested_none_stripping(self):
        payload = {"outer": {"inner": {"val": None, "keep": 42}}}
        out = json.loads(compress_payload(payload))
        assert "val" not in out["outer"]["inner"]
        assert out["outer"]["inner"]["keep"] == 42


class TestIsOhlcvLike:
    def test_detects_ohlcv_keys(self):
        rows = [{"open": 1, "high": 2, "low": 0.9, "close": 1.5}]
        assert _is_ohlcv_like(rows)

    def test_rejects_non_ohlcv(self):
        rows = [{"name": "foo", "age": 30, "email": "a@b.com"}]
        assert not _is_ohlcv_like(rows)


class TestCompressText:
    def test_collapses_multiple_blank_lines(self):
        text = "line1\n\n\n\nline2"
        result = compress_text(text)
        assert "\n\n\n" not in result
        assert "line1" in result
        assert "line2" in result

    def test_deduplicates_consecutive_lines(self):
        text = "header\nitem\nitem\nitem\nfooter"
        result = compress_text(text)
        assert result.count("item") == 1

    def test_truncates_at_max_chars(self):
        text = "x" * 10000
        result = compress_text(text, max_chars=500)
        assert len(result) <= 515  # 500 + truncation marker
        assert "truncated" in result

    def test_empty_string_passthrough(self):
        assert compress_text("") == ""

    def test_preserves_meaningful_content(self):
        text = "Pre-market review:\n  QQQ bull_put_spread -$22\n  39 DTE\nBe direct."
        result = compress_text(text)
        assert "QQQ" in result
        assert "39 DTE" in result
