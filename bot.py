import time
import logging
import json
import os
import requests
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────
API_KEY    = 'PKN2RA6WXSFVWCCQRCMXKBOLCB'
SECRET_KEY = 'Ca2gPCip7Sru6K16G3ZL4NDwhLBPstkh8ePuF1CDfUPo'
BASE_URL   = 'https://paper-api.alpaca.markets'
DATA_URL   = 'https://data.alpaca.markets'

LONG_TERM  = ['SPY', 'QQQ', 'VTI']
SHORT_TERM = ['NVDA', 'TSLA', 'AMD', 'MSFT', 'AAPL', 'META', 'AMZN']

TAKE_PROFIT_PCT  = 0.08
STOP_LOSS_PCT    = 0.04
FLAG_THRESHOLD   = 0.05
LONG_ALLOCATION  = 0.30
SHORT_ALLOCATION = 0.10
SCAN_INTERVAL    = 300  # seconds (5 min)

HEADERS = {
    'APCA-API-KEY-ID': API_KEY,
    'APCA-API-SECRET-KEY': SECRET_KEY,
    'Content-Type': 'application/json'
}

# Simple trade log stored in memory + flat file
TRADE_LOG_FILE = 'trades.json'

def load_trades():
    if os.path.exists(TRADE_LOG_FILE):
        with open(TRADE_LOG_FILE) as f:
            return json.load(f)
    return []

def save_trades(trades):
    with open(TRADE_LOG_FILE, 'w') as f:
        json.dump(trades, f)

# ─── ALPACA API ───────────────────────────────────────────
def alpaca(path, method='GET', body=None):
    url = BASE_URL + path
    r = requests.request(method, url, headers=HEADERS, json=body, timeout=10)
    r.raise_for_status()
    return r.json()

def alpaca_data(path):
    url = DATA_URL + path
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()

# ─── NEWS SENTIMENT VIA CLAUDE ───────────────────────────
def analyse_sentiment(tickers):
    headlines = []
    try:
        url = f"{DATA_URL}/v1beta1/news?symbols={','.join(tickers)}&limit=20"
        r = requests.get(url, headers=HEADERS, timeout=10)
        data = r.json()
        headlines = [
            f"[{','.join(n.get('symbols', []))}] {n['headline']}"
            for n in data.get('news', [])[:12]
        ]
    except Exception as e:
        log.warning(f"News fetch failed: {e}")
        headlines = [f"[{t}] {t} shows market momentum" for t in tickers[:6]]

    prompt = f"""You are a quantitative stock trading AI. Analyse these headlines and return ONLY a JSON array (no markdown):
[{{"ticker":"SYMBOL","sentiment":"bullish"|"bearish"|"neutral","score":0.0-1.0,"action":"buy"|"sell"|"hold","reason":"one sentence","urgency":"high"|"medium"|"low"}}]

Headlines:
{chr(10).join(headlines)}

Rules:
- action=buy only if bullish AND score>=0.7
- action=sell only if bearish AND score>=0.7
- Only use tickers from: {','.join(tickers)}
- Max 8 items"""

    try:
        r = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'Content-Type': 'application/json'},
            json={
                'model': 'claude-sonnet-4-6',
                'max_tokens': 800,
                'messages': [{'role': 'user', 'content': prompt}]
            },
            timeout=30
        )
        text = r.json()['content'][0]['text']
        text = text.replace('```json', '').replace('```', '').strip()
        return json.loads(text)
    except Exception as e:
        log.warning(f"AI analysis failed: {e}")
        return []

# ─── TRADE EXECUTION ─────────────────────────────────────
def get_price(ticker):
    try:
        data = alpaca_data(f'/v2/stocks/{ticker}/snapshot')
        return data.get('latestTrade', {}).get('p') or data.get('minuteBar', {}).get('c')
    except:
        return None

def buy(ticker, budget_usd, trades):
    price = get_price(ticker)
    if not price:
        log.warning(f"No price for {ticker}")
        return False
    qty = int(budget_usd / price)
    if qty < 1:
        log.warning(f"{ticker}: budget too small (${budget_usd:.2f} at ${price:.2f})")
        return False
    try:
        alpaca('/v2/orders', 'POST', {
            'symbol': ticker, 'qty': qty,
            'side': 'buy', 'type': 'market', 'time_in_force': 'day'
        })
        log.info(f"✓ BUY {qty}x {ticker} @ ~${price:.2f}")
        return True
    except Exception as e:
        log.error(f"Buy failed {ticker}: {e}")
        return False

def sell(ticker, pl_pct, trades):
    try:
        # Get current qty
        pos = alpaca(f'/v2/positions/{ticker}')
        qty = pos['qty']
        alpaca('/v2/orders', 'POST', {
            'symbol': ticker, 'qty': qty,
            'side': 'sell', 'type': 'market', 'time_in_force': 'day'
        })
        trades.append({'ticker': ticker, 'pl_pct': pl_pct, 'ts': time.time()})
        save_trades(trades)
        log.info(f"✓ SELL {ticker} | P&L: {pl_pct*100:.2f}%")
        return True
    except Exception as e:
        log.error(f"Sell failed {ticker}: {e}")
        return False

# ─── POSITION HEALTH ─────────────────────────────────────
def check_positions(trades):
    try:
        positions = alpaca('/v2/positions')
    except Exception as e:
        log.error(f"Positions fetch failed: {e}")
        return []

    for pos in positions:
        ticker = pos['symbol']
        pl     = float(pos['unrealized_plpc'])

        if pl <= -FLAG_THRESHOLD and pl > -STOP_LOSS_PCT:
            log.warning(f"FLAG: {ticker} down {pl*100:.1f}% — near stop loss")

        if pl <= -STOP_LOSS_PCT:
            log.warning(f"STOP LOSS: {ticker} ({pl*100:.1f}%) — selling")
            sell(ticker, pl, trades)

        elif pl >= TAKE_PROFIT_PCT:
            log.info(f"TAKE PROFIT: {ticker} (+{pl*100:.1f}%) — selling")
            sell(ticker, pl, trades)

    return positions

# ─── STATS SUMMARY ───────────────────────────────────────
def print_stats(trades):
    wins   = [t for t in trades if t['pl_pct'] > 0]
    losses = [t for t in trades if t['pl_pct'] <= 0]
    total  = len(trades)
    rate   = round(len(wins)/total*100) if total > 0 else 0
    avg_gain = sum(t['pl_pct'] for t in wins)/len(wins)*100 if wins else 0
    log.info(f"── Stats: {total} trades | Win rate: {rate}% | Avg gain: {avg_gain:.2f}%")

# ─── MARKET HOURS CHECK ──────────────────────────────────
def market_is_open():
    try:
        clock = alpaca('/v2/clock')
        return clock.get('is_open', False)
    except:
        return False

# ─── MAIN LOOP ───────────────────────────────────────────
def main():
    log.info("AutoTrader v2 starting up...")
    trades = load_trades()

    while True:
        try:
            if not market_is_open():
                log.info("Market closed — sleeping 10 min")
                time.sleep(600)
                continue

            log.info("── Scan started ──")

            # Account info
            account = alpaca('/v2/account')
            equity  = float(account['equity'])
            bp      = float(account['buying_power'])
            log.info(f"Equity: ${equity:,.2f} | Buying power: ${bp:,.2f}")

            # Position health
            positions    = check_positions(trades)
            owned        = {p['symbol'] for p in positions}

            # Sentiment analysis
            all_tickers  = SHORT_TERM + LONG_TERM
            signals      = analyse_sentiment(all_tickers)

            for sig in signals:
                ticker = sig.get('ticker')
                action = sig.get('action')
                score  = float(sig.get('score', 0))
                reason = sig.get('reason', '')

                if not ticker:
                    continue

                if action == 'buy' and ticker not in owned and score >= 0.7:
                    is_long = ticker in LONG_TERM
                    budget  = bp * (LONG_ALLOCATION / len(LONG_TERM) if is_long else SHORT_ALLOCATION)
                    log.info(f"Signal BUY {ticker} (score={score}) — {reason}")
                    if buy(ticker, budget, trades):
                        owned.add(ticker)

                elif action == 'sell' and ticker in owned:
                    pos    = next((p for p in positions if p['symbol'] == ticker), None)
                    pl_pct = float(pos['unrealized_plpc']) if pos else 0
                    log.info(f"Signal SELL {ticker} — {reason}")
                    sell(ticker, pl_pct, trades)
                    owned.discard(ticker)

            print_stats(trades)
            log.info(f"── Scan done. Sleeping {SCAN_INTERVAL//60} min ──")

        except Exception as e:
            log.error(f"Main loop error: {e}")

        time.sleep(SCAN_INTERVAL)

if __name__ == '__main__':
    main()
