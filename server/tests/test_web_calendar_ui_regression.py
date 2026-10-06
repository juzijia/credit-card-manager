from pathlib import Path


WEB_INDEX = Path(__file__).resolve().parents[2] / "web" / "index.html"


def _web_index_text() -> str:
    return WEB_INDEX.read_text(encoding="utf-8")


def test_repayment_calendar_does_not_restore_removed_filter_dropdowns() -> None:
    html = _web_index_text()

    assert "统计累计账单 (范围)" not in html
    assert "统计单日账单 (单天)" not in html
    assert "全部账单 (含手工)" not in html


def test_repayment_calendar_selected_day_does_not_use_black_circle_style() -> None:
    html = _web_index_text()

    assert "bg-slate-900 text-white dark:bg-blue-600 dark:text-white font-bold shadow-xs" not in html


def test_statement_detail_analysis_ui_contract() -> None:
    html = _web_index_text()

    # Single statement 6-card metrics removed per user requirement
    assert "statementAnalyses[stmt.id].metrics.gross_purchase_minor" not in html

    # Monthly view breakdown contract
    assert "信用卡账单" in html
    assert "自然月明细" in html


def test_statement_detail_semantic_correction_and_409_handling() -> None:
    html = _web_index_text()

    assert "/transactions/' + transaction.id + '/semantics" in html
    assert "correctTransactionType" in html
    assert "无法修改分类" in html
    assert "transactionTypeOptions" in html
    for t_type in [
        "purchase", "refund", "repayment", "fee", "interest",
        "annual_fee", "reward", "cash_advance", "reversal", "adjustment", "unknown"
    ]:
        assert f"value: '{t_type}'" in html
