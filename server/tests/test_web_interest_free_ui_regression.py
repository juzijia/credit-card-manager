import json
import subprocess
from pathlib import Path


WEB_INDEX = Path(__file__).resolve().parents[2] / "web" / "index.html"


def _web_index_text() -> str:
    return WEB_INDEX.read_text(encoding="utf-8")


def test_web_index_serves_financial_ssot(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "repayment_card_id=" in response.text
    assert "暂无可靠估算" in response.text


def test_interest_free_web_ui_template_contract() -> None:
    html = _web_index_text()

    # Core features & labels
    assert "今天刷哪张卡" in html
    assert "今日推荐" in html
    assert "今日免息" in html
    assert "免息期暂不可计算" in html
    assert "其次备选" in html

    # API endpoints
    assert "/interest-free-estimate?spend_date=" in html

    # No forbidden internal/debug terms in user-facing card guidance
    card_view_section = html[html.find("activeTab === 'cards'"):html.find("activeTab === 'annual_fee'")]
    for forbidden in [
        "T+0", "T+1", "T+2", "posting_dependent", "unresolved",
        "fully_estimated", "selected_statement_date", "cycle uncertain",
    ]:
        assert forbidden not in card_view_section


def _run_node_script(js_code: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", js_code, "{}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(proc.stdout)


def test_interest_free_get_card_display_available_and_max() -> None:
    # Extracts real getCardInterestFreeDisplay from web/index.html and tests behavior
    js = """
    const fs = require('fs');
    const vm = require('vm');
    const path = require('path');
    const html = fs.readFileSync(path.resolve('web/index.html'), 'utf8');
    const dStart = html.indexOf('function getCardInterestFreeDisplay(cardId) {');
    const dEnd = html.indexOf('const recommendedCards = computed', dStart);
    const fnSrc = html.slice(dStart, dEnd);

    const state = {
      cardInterestFree: { value: {
        card1: {
          available: true,
          estimate: {
            conservative_interest_free_days: 43,
            theoretical_max_interest_free_days: 49,
          }
        },
        card2: {
          available: true,
          estimate: {
            conservative_interest_free_days: 25,
            theoretical_max_interest_free_days: null,
          }
        },
        card3: {
          available: false,
          reason: 'missing_statement_day',
        }
      }},
      loadingCardInterestFree: { value: false }
    };

    const sandbox = { ...state };
    vm.createContext(sandbox);
    vm.runInContext(fnSrc, sandbox);

    const res1 = sandbox.getCardInterestFreeDisplay('card1');
    const res2 = sandbox.getCardInterestFreeDisplay('card2');
    const res3 = sandbox.getCardInterestFreeDisplay('card3');
    sandbox.loadingCardInterestFree.value = true;
    const resLoading = sandbox.getCardInterestFreeDisplay('card_unknown');

    console.log(JSON.stringify({ res1, res2, res3, resLoading }));
    """
    data = _run_node_script(js)

    # 1. available card -> shows conservative days
    assert data["res1"]["available"] is True
    assert data["res1"]["conservativeDays"] == 43
    # 2. theoretical_max has value -> returns value
    assert data["res1"]["theoreticalMaxDays"] == 49

    # 3. theoretical_max is null -> returns null, never invalid text
    assert data["res2"]["available"] is True
    assert data["res2"]["conservativeDays"] == 25
    assert data["res2"]["theoreticalMaxDays"] is None

    # 4. unavailable card -> returns "免息期暂不可计算"
    assert data["res3"]["available"] is False
    assert data["res3"]["text"] == "免息期暂不可计算"

    # Loading state
    assert data["resLoading"]["loading"] is True
    assert data["resLoading"]["text"] == "计算中..."


def test_interest_free_recommended_cards_ranking_and_filters() -> None:
    # Extracts real recommendedCards computed property from web/index.html
    js = """
    const fs = require('fs');
    const vm = require('vm');
    const path = require('path');
    const html = fs.readFileSync(path.resolve('web/index.html'), 'utf8');
    const rStart = html.indexOf('const recommendedCards = computed(() => {');
    const rEnd = html.indexOf('async function loadTxnInterestFree', rStart);
    const rSrc = html.slice(rStart, rEnd);

    const cards = [
      { id: 'c1', bank_name: '招商银行', last4: '9492', status: 'active', enabled: true },
      { id: 'c2', bank_name: '农业银行', last4: '5797', status: 'active', enabled: true },
      { id: 'c3', bank_name: '广发银行', last4: '1721', status: 'active', enabled: true },
      { id: 'c_disabled', bank_name: '工商银行', last4: '0001', status: 'active', enabled: false },
      { id: 'c_closed', bank_name: '建设银行', last4: '0002', status: 'closed', enabled: true },
      { id: 'c_failed', bank_name: '中国银行', last4: '0003', status: 'active', enabled: true },
      { id: 'c_tie_a', bank_name: '浦发银行', last4: '1111', status: 'active', enabled: true },
      { id: 'c_tie_b', bank_name: '中信银行', last4: '2222', status: 'active', enabled: true },
    ];

    const cardInterestFree = {
      // c1: theoretical_max = 56, but conservative = 43
      c1: { available: true, estimate: { conservative_interest_free_days: 43, theoretical_max_interest_free_days: 56 } },
      // c2: theoretical_max = 54, conservative = 54 -> MUST rank #1 ahead of c1
      c2: { available: true, estimate: { conservative_interest_free_days: 54, theoretical_max_interest_free_days: 54 } },
      // c3: conservative = 41
      c3: { available: true, estimate: { conservative_interest_free_days: 41, theoretical_max_interest_free_days: 50 } },
      // disabled & closed
      c_disabled: { available: true, estimate: { conservative_interest_free_days: 60 } },
      c_closed: { available: true, estimate: { conservative_interest_free_days: 60 } },
      // failed API
      c_failed: { available: false, error: true },
      // tied conservative days: both 30
      c_tie_a: { available: true, estimate: { conservative_interest_free_days: 30 } },
      c_tie_b: { available: true, estimate: { conservative_interest_free_days: 30 } },
    };

    const sandbox = {
      computed: fn => ({ get value() { return fn(); } }),
      cards: { value: cards },
      cardInterestFree: { value: cardInterestFree },
    };
    const rec = vm.runInNewContext(rSrc + '; recommendedCards.value', sandbox);
    console.log(JSON.stringify({
      ids: rec.map(r => r.id),
      days: rec.map(r => cardInterestFree[r.id].estimate.conservative_interest_free_days),
    }));
    """
    result = _run_node_script(js)
    ids = result["ids"]

    # Requirement 5: Sorted by conservative days DESC (54 > 43 > 41 > 30 == 30)
    assert ids[0] == "c2"  # 农业银行 54天
    assert ids[1] == "c1"  # 招商银行 43天
    assert ids[2] == "c3"  # 广发银行 41天

    # Requirement 6: c1 has larger theoretical_max (56) than c2 (54), but lower conservative (43 < 54).
    # c1 must NOT rank first!
    assert ids[0] != "c1"

    # Requirement 7: c_failed API error does not break others
    assert "c_failed" not in ids

    # Requirement 8: inactive and disabled cards are excluded
    assert "c_disabled" not in ids
    assert "c_closed" not in ids

    # Requirement 9: tied days maintain stable relative order (c_tie_a before c_tie_b)
    assert ids.index("c_tie_a") < ids.index("c_tie_b")


def test_interest_free_responsive_classes() -> None:
    # Requirement 11: Mobile & desktop responsive layout classes
    html = _web_index_text()
    assert "flex flex-col sm:flex-row sm:items-center justify-between gap-3" in html
    assert "truncate" in html
    assert "grid grid-cols-1 md:grid-cols-2 xl:grid-cols-" in html
    assert "grid grid-cols-3 gap-x-4" in html
