import time
import logging
import json
import os
import threading
import requests
from datetime import datetime
from flask import Flask, jsonify, render_template_string

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
SCAN_INTERVAL    = 300

HEADERS = {
    'APCA-API-KEY-ID': API_KEY,
    'APCA-API-SECRET-KEY': SECRET_KEY,
    'Content-Type': 'application/json'
}

TRADE_LOG_FILE   = 'trades.json'
EQUITY_LOG_FILE  = 'equity.json'

# ─── STATE (shared between bot thread and web server) ─────
state = {
    'status': 'Starting...',
    'equity': 0,
    'buying_power': 0,
    'today_pl': 0,
    'today_pl_pct': 0,
    'positions': [],
    'signals': [],
    'alerts': [],
    'log': [],
    'last_scan': None,
    'market_open': False,
}

def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f)

trades       = load_json(TRADE_LOG_FILE, [])
equity_hist  = load_json(EQUITY_LOG_FILE, [])  # [{ts, val}]

def add_log(msg, level='info'):
    ts = datetime.now().strftime('%H:%M:%S')
    entry = {'ts': ts, 'msg': msg, 'level': level}
    state['log'].insert(0, entry)
    state['log'] = state['log'][:60]
    if level == 'error':   log.error(msg)
    elif level == 'warn':  log.warning(msg)
    else:                  log.info(msg)

def add_alert(kind, msg):
    ts = datetime.now().strftime('%H:%M:%S')
    state['alerts'].insert(0, {'kind': kind, 'msg': msg, 'ts': ts})
    state['alerts'] = state['alerts'][:20]

# ─── ALPACA ───────────────────────────────────────────────
def alpaca(path, method='GET', body=None):
    r = requests.request(method, BASE_URL + path, headers=HEADERS, json=body, timeout=10)
    r.raise_for_status()
    return r.json()

def alpaca_data(path):
    r = requests.get(DATA_URL + path, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()

def market_is_open():
    try:
        return alpaca('/v2/clock').get('is_open', False)
    except:
        return False

def get_price(ticker):
    try:
        d = alpaca_data(f'/v2/stocks/{ticker}/snapshot')
        return d.get('latestTrade', {}).get('p') or d.get('minuteBar', {}).get('c')
    except:
        return None

# ─── SENTIMENT ───────────────────────────────────────────
def analyse_sentiment(tickers):
    headlines = []
    try:
        r = requests.get(
            f"{DATA_URL}/v1beta1/news?symbols={','.join(tickers)}&limit=20",
            headers=HEADERS, timeout=10
        )
        headlines = [f"[{','.join(n.get('symbols',[]))}] {n['headline']}"
                     for n in r.json().get('news', [])[:12]]
    except Exception as e:
        add_log(f"News fetch failed: {e}", 'warn')
        headlines = [f"[{t}] {t} market momentum signal" for t in tickers[:6]]

    prompt = f"""You are a quantitative stock trading AI. Analyse these headlines and return ONLY a JSON array (no markdown):
[{{"ticker":"SYMBOL","headline":"brief headline","sentiment":"bullish"|"bearish"|"neutral","score":0.0-1.0,"action":"buy"|"sell"|"hold","reason":"one sentence","urgency":"high"|"medium"|"low"}}]
Headlines:
{chr(10).join(headlines)}
Rules: action=buy only if bullish AND score>=0.7, action=sell only if bearish AND score>=0.7, only tickers from: {','.join(tickers)}, max 8 items"""

    try:
        r = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'Content-Type': 'application/json'},
            json={'model':'claude-sonnet-4-6','max_tokens':800,
                  'messages':[{'role':'user','content':prompt}]},
            timeout=30
        )
        text = r.json()['content'][0]['text'].replace('```json','').replace('```','').strip()
        return json.loads(text)
    except Exception as e:
        add_log(f"AI analysis failed: {e}", 'warn')
        return []

# ─── TRADING ─────────────────────────────────────────────
def do_buy(ticker, budget):
    price = get_price(ticker)
    if not price: return False
    qty = int(budget / price)
    if qty < 1:
        add_log(f"{ticker}: budget too small (${budget:.0f} @ ${price:.2f})", 'warn')
        return False
    try:
        alpaca('/v2/orders','POST',{'symbol':ticker,'qty':qty,'side':'buy','type':'market','time_in_force':'day'})
        add_log(f"✓ BUY {qty}x {ticker} @ ~${price:.2f}", 'gain')
        add_alert('buy', f"Bought {qty}x {ticker} @ ~${price:.2f}")
        return True
    except Exception as e:
        add_log(f"Buy failed {ticker}: {e}", 'error')
        return False

def do_sell(ticker, pl_pct):
    try:
        pos = alpaca(f'/v2/positions/{ticker}')
        alpaca('/v2/orders','POST',{'symbol':ticker,'qty':pos['qty'],'side':'sell','type':'market','time_in_force':'day'})
        trades.append({'ticker':ticker,'pl_pct':pl_pct,'ts':time.time()})
        save_json(TRADE_LOG_FILE, trades)
        label = f"+{pl_pct*100:.2f}%" if pl_pct > 0 else f"{pl_pct*100:.2f}%"
        add_log(f"✓ SELL {ticker} | P&L: {label}", 'gain' if pl_pct > 0 else 'warn')
        add_alert('sell' if pl_pct < 0 else 'profit', f"Sold {ticker} at {label}")
        return True
    except Exception as e:
        add_log(f"Sell failed {ticker}: {e}", 'error')
        return False

def check_positions():
    try:
        positions = alpaca('/v2/positions')
        state['positions'] = positions
        for pos in positions:
            ticker = pos['symbol']
            pl     = float(pos['unrealized_plpc'])
            if pl <= -FLAG_THRESHOLD and pl > -STOP_LOSS_PCT:
                add_log(f"FLAG: {ticker} down {pl*100:.1f}%", 'warn')
                add_alert('flag', f"{ticker} down {pl*100:.1f}% — near stop loss")
            if pl <= -STOP_LOSS_PCT:
                add_log(f"STOP LOSS: {ticker} ({pl*100:.1f}%)", 'warn')
                do_sell(ticker, pl)
            elif pl >= TAKE_PROFIT_PCT:
                add_log(f"TAKE PROFIT: {ticker} (+{pl*100:.1f}%)", 'gain')
                do_sell(ticker, pl)
        return positions
    except Exception as e:
        add_log(f"Position check failed: {e}", 'error')
        return []

# ─── MAIN BOT LOOP ───────────────────────────────────────
def bot_loop():
    global equity_hist
    add_log("AutoTrader v2 starting up...", 'info')

    while True:
        try:
            open_ = market_is_open()
            state['market_open'] = open_

            if not open_:
                state['status'] = 'Market closed'
                add_log("Market closed — sleeping 10 min", 'info')
                time.sleep(600)
                continue

            state['status'] = 'Scanning...'
            add_log("── Scan started ──", 'info')

            acc    = alpaca('/v2/account')
            equity = float(acc['equity'])
            last   = float(acc['last_equity'])
            bp     = float(acc['buying_power'])
            pl     = equity - last
            pl_pct = pl / last * 100

            state.update({'equity':equity,'buying_power':bp,'today_pl':pl,'today_pl_pct':pl_pct})

            equity_hist.append({'ts': int(time.time()*1000), 'val': equity})
            equity_hist = equity_hist[-500:]
            save_json(EQUITY_LOG_FILE, equity_hist)

            positions = check_positions()
            owned     = {p['symbol'] for p in positions}

            signals = analyse_sentiment(SHORT_TERM + LONG_TERM)
            state['signals'] = signals

            for sig in signals:
                ticker = sig.get('ticker')
                action = sig.get('action')
                score  = float(sig.get('score', 0))
                if not ticker: continue
                if action == 'buy' and ticker not in owned and score >= 0.7:
                    is_long = ticker in LONG_TERM
                    budget  = bp * (LONG_ALLOCATION/len(LONG_TERM) if is_long else SHORT_ALLOCATION)
                    add_log(f"Signal BUY {ticker} ({sig.get('urgency','?')} urgency) — {sig.get('reason','')}", 'info')
                    if do_buy(ticker, budget):
                        owned.add(ticker)
                elif action == 'sell' and ticker in owned:
                    pos    = next((p for p in positions if p['symbol']==ticker), None)
                    pl_pct_pos = float(pos['unrealized_plpc']) if pos else 0
                    add_log(f"Signal SELL {ticker} — {sig.get('reason','')}", 'warn')
                    do_sell(ticker, pl_pct_pos)

            wins  = [t for t in trades if t['pl_pct'] > 0]
            total = len(trades)
            rate  = round(len(wins)/total*100) if total > 0 else 0
            state['status'] = f"Running | Win rate: {rate}% | {total} trades"
            state['last_scan'] = datetime.now().strftime('%H:%M:%S')
            add_log(f"── Scan done. Win rate: {rate}% ({total} trades) ──", 'info')

        except Exception as e:
            add_log(f"Loop error: {e}", 'error')
            state['status'] = 'Error — retrying'

        time.sleep(SCAN_INTERVAL)

# ─── FLASK WEB SERVER ────────────────────────────────────
app = Flask(__name__)

DASHBOARD = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>AutoTrader v2</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Space+Mono:wght@400;700&display=swap');
:root{--bg:#0a0a0f;--s:#111118;--s2:#1a1a24;--b:#ffffff0f;--a:#7c6bff;--a2:#00e5a0;--t:#f0f0ff;--m:#6b6b8a;--g:#00e5a0;--l:#ff4d6d;--w:#ffb547}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--t);font-family:'Space Grotesk',sans-serif;padding:20px;min-height:100vh}
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid var(--b)}
.logo{font-family:'Space Mono',monospace;font-size:18px;font-weight:700;color:var(--a)}.logo span{color:var(--a2)}
.pill{display:flex;align-items:center;gap:8px;background:var(--s2);border:1px solid var(--b);border-radius:20px;padding:5px 12px;font-size:12px;color:var(--m)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--m)}
.dot.on{background:var(--g);box-shadow:0 0 8px var(--g);animation:p 2s infinite}
.dot.warn{background:var(--w);box-shadow:0 0 8px var(--w)}
@keyframes p{0%,100%{opacity:1}50%{opacity:.4}}
.grid6{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:18px}
.card{background:var(--s);border:1px solid var(--b);border-radius:12px;padding:16px}
.lbl{font-size:10px;text-transform:uppercase;letter-spacing:1.5px;color:var(--m);margin-bottom:6px}
.val{font-family:'Space Mono',monospace;font-size:20px;font-weight:700}
.val.g{color:var(--g)}.val.l{color:var(--l)}
.sub{font-size:11px;color:var(--m);margin-top:4px}
.chart-row{display:grid;grid-template-columns:1fr 240px;gap:14px;margin-bottom:18px}
.chart-card{background:var(--s);border:1px solid var(--b);border-radius:12px;padding:18px}
.ch{font-size:13px;font-weight:600;margin-bottom:14px;display:flex;justify-content:space-between;align-items:center}
.chart-wrap{position:relative;height:160px}
.wr-wrap{position:relative;height:120px;display:flex;align-items:center;justify-content:center}
.wr-center{position:absolute;text-align:center}
.wr-pct{font-family:'Space Mono',monospace;font-size:22px;font-weight:700;color:var(--g)}
.wr-lbl{font-size:9px;color:var(--m)}
.tstats{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}
.ts{background:var(--s2);border-radius:8px;padding:9px;text-align:center}
.tsv{font-family:'Space Mono',monospace;font-size:14px;font-weight:700}
.tsl{font-size:9px;color:var(--m);margin-top:2px}
.main{display:grid;grid-template-columns:1fr 340px;gap:14px}
.sec{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:var(--m);margin-bottom:10px;display:flex;align-items:center;gap:8px}
.sec::after{content:'';flex:1;height:1px;background:var(--b)}
.nfeed,.plist,.alist{display:flex;flex-direction:column;gap:8px}
.ni{background:var(--s2);border:1px solid var(--b);border-radius:10px;padding:12px 14px;display:flex;gap:10px}
.badge{font-family:'Space Mono',monospace;font-size:9px;font-weight:700;padding:3px 7px;border-radius:5px;white-space:nowrap;flex-shrink:0}
.badge.bullish{background:#00e5a015;color:var(--g);border:1px solid #00e5a030}
.badge.bearish{background:#ff4d6d15;color:var(--l);border:1px solid #ff4d6d30}
.badge.neutral{background:#fff1;color:var(--m);border:1px solid var(--b)}
.nh{font-size:12px;font-weight:500;line-height:1.4;margin-bottom:3px}
.nm{font-size:10px;color:var(--m);display:flex;gap:8px;flex-wrap:wrap}
.nt{font-family:'Space Mono',monospace;color:var(--a)}
.sb{width:50px;height:3px;background:var(--b);border-radius:2px;overflow:hidden;margin-top:5px}
.sf{height:100%;border-radius:2px;background:var(--g)}.sf.b{background:var(--l)}
.pi{background:var(--s2);border:1px solid var(--b);border-radius:10px;padding:12px 14px}
.pt{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.ptk{font-family:'Space Mono',monospace;font-size:14px;font-weight:700}
.ptyp{font-size:9px;padding:2px 7px;border-radius:4px;background:var(--a)20;color:var(--a);border:1px solid var(--a)40}
.ptyp.lt{background:var(--a2)15;color:var(--a2);border-color:var(--a2)40}
.ps{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}
.psl{color:var(--m);font-size:9px;margin-bottom:2px}
.psv{font-family:'Space Mono',monospace;font-size:12px;font-weight:700}
.pt2{height:3px;background:var(--b);border-radius:2px;margin-top:8px;overflow:hidden}
.pf2{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--a),var(--a2))}
.ai{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border-radius:8px;border-left:3px solid;background:var(--s2)}
.ai.flag{border-color:var(--w)}.ai.buy{border-color:var(--g)}.ai.sell{border-color:var(--l)}.ai.profit{border-color:var(--a2)}.ai.info{border-color:var(--a)}
.aicon{font-size:14px;flex-shrink:0}.atxt{font-size:11px;line-height:1.4}.ats{font-size:10px;color:var(--m);margin-top:2px}
.log-box{background:var(--s);border:1px solid var(--b);border-radius:10px;padding:12px;margin-top:14px}
.log-entries{font-family:'Space Mono',monospace;font-size:10px;color:var(--m);line-height:1.9;max-height:160px;overflow-y:auto}
.log-entries .lg{color:var(--g)}.log-entries .lw{color:var(--w)}.log-entries .li{color:var(--a)}.log-entries .le{color:var(--l)}
.empty{text-align:center;padding:24px;color:var(--m);font-size:12px}
.mode{font-family:'Space Mono',monospace;font-size:10px;padding:5px 10px;background:var(--w)15;color:var(--w);border:1px solid var(--w)30;border-radius:6px}
.refresh{font-size:11px;color:var(--m);margin-left:auto}
::-webkit-scrollbar{width:3px}::-webkit-scrollbar-thumb{background:var(--b);border-radius:2px}
@media(max-width:1000px){.grid6{grid-template-columns:repeat(3,1fr)}.chart-row,.main{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <div class="logo">AUTO<span>TRADER</span></div>
  <div style="display:flex;gap:10px;align-items:center">
    <span class="mode" id="modeTag">📄 PAPER</span>
    <div class="pill"><div class="dot" id="dot"></div><span id="statusTxt">Loading...</span></div>
  </div>
</header>

<div class="grid6">
  <div class="card"><div class="lbl">Portfolio</div><div class="val" id="eq">—</div><div class="sub">Paper equity</div></div>
  <div class="card"><div class="lbl">Today P&L</div><div class="val" id="tpl">—</div><div class="sub" id="tplsub">vs yesterday</div></div>
  <div class="card"><div class="lbl">Weekly P&L</div><div class="val" id="wpl">—</div><div class="sub">Last 7 days</div></div>
  <div class="card"><div class="lbl">Monthly P&L</div><div class="val" id="mpl">—</div><div class="sub">Last 30 days</div></div>
  <div class="card"><div class="lbl">Buying Power</div><div class="val" id="bp">—</div><div class="sub">Available</div></div>
  <div class="card"><div class="lbl">Positions</div><div class="val" id="npos">—</div><div class="sub">Open trades</div></div>
</div>

<div class="chart-row">
  <div class="chart-card">
    <div class="ch"><span>Portfolio P&L</span><span class="refresh" id="lastScan">Last scan: —</span></div>
    <div class="chart-wrap"><canvas id="plc"></canvas></div>
  </div>
  <div class="chart-card">
    <div class="ch">Win Rate</div>
    <div class="wr-wrap">
      <canvas id="wc" width="120" height="120"></canvas>
      <div class="wr-center"><div class="wr-pct" id="wrPct">—</div><div class="wr-lbl">WIN RATE</div></div>
    </div>
    <div class="tstats">
      <div class="ts"><div class="tsv" id="tot">0</div><div class="tsl">Trades</div></div>
      <div class="ts"><div class="tsv" style="color:var(--g)" id="wins">0W</div><div class="tsl">Wins</div></div>
      <div class="ts"><div class="tsv" style="color:var(--l)" id="losses">0L</div><div class="tsl">Losses</div></div>
      <div class="ts"><div class="tsv" style="color:var(--a2)" id="avgg">—</div><div class="tsl">Avg Gain</div></div>
    </div>
  </div>
</div>

<div class="main">
  <div>
    <div class="sec">Live Signals</div>
    <div class="nfeed" id="nfeed"><div class="empty">Waiting for scan...</div></div>
    <div class="sec" style="margin-top:18px">Open Positions</div>
    <div class="plist" id="plist"><div class="empty">No open positions</div></div>
  </div>
  <div>
    <div class="sec">Alerts</div>
    <div class="alist" id="alist"><div class="empty">No alerts yet</div></div>
    <div class="sec" style="margin-top:14px">Activity Log</div>
    <div class="log-box"><div class="log-entries" id="logbox"><div>Loading...</div></div></div>
  </div>
</div>

<script>
let plChart=null, wChart=null;

function fmt(v){return v>=0?'+$'+v.toFixed(2):'−$'+Math.abs(v).toFixed(2)}
function fmtPct(v){return v>=0?'+'+v.toFixed(2)+'%':v.toFixed(2)+'%'}
function cls(v){return v>=0?'g':'l'}

async function load(){
  const d = await fetch('/api').then(r=>r.json());

  // Status
  document.getElementById('dot').className='dot '+(d.market_open?'on':'warn');
  document.getElementById('statusTxt').textContent=d.status;
  if(d.last_scan) document.getElementById('lastScan').textContent='Last scan: '+d.last_scan;

  // Stats
  document.getElementById('eq').textContent='$'+d.equity.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  const tpl=document.getElementById('tpl');
  tpl.textContent=fmt(d.today_pl)+' ('+fmtPct(d.today_pl_pct)+')';
  tpl.className='val '+cls(d.today_pl);
  document.getElementById('bp').textContent='$'+d.buying_power.toFixed(2);
  document.getElementById('npos').textContent=d.positions.length;

  const wpl=document.getElementById('wpl');
  wpl.textContent=d.week_pl!==null?fmt(d.week_pl):'Not enough data';
  if(d.week_pl!==null) wpl.className='val '+cls(d.week_pl);

  const mpl=document.getElementById('mpl');
  mpl.textContent=d.month_pl!==null?fmt(d.month_pl):'Not enough data';
  if(d.month_pl!==null) mpl.className='val '+cls(d.month_pl);

  // P&L Chart
  if(!plChart){
    const ctx=document.getElementById('plc').getContext('2d');
    plChart=new Chart(ctx,{type:'line',data:{labels:[],datasets:[{label:'Equity',data:[],borderColor:'#7c6bff',backgroundColor:'rgba(124,107,255,0.08)',borderWidth:2,pointRadius:2,tension:0.4,fill:true}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>'$'+c.parsed.y.toFixed(2)}}},
    scales:{x:{grid:{color:'#ffffff08'},ticks:{color:'#6b6b8a',font:{size:10},maxTicksLimit:8}},y:{grid:{color:'#ffffff08'},ticks:{color:'#6b6b8a',font:{size:10},callback:v=>'$'+v.toFixed(0)}}}}});
  }
  if(d.equity_hist && d.equity_hist.length>0){
    const labels=d.equity_hist.map(e=>{const dt=new Date(e.ts);return dt.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})});
    const vals=d.equity_hist.map(e=>e.val);
    const trend=vals.length>1?vals[vals.length-1]-vals[0]:0;
    plChart.data.labels=labels;
    plChart.data.datasets[0].data=vals;
    plChart.data.datasets[0].borderColor=trend>=0?'#00e5a0':'#ff4d6d';
    plChart.data.datasets[0].backgroundColor=trend>=0?'rgba(0,229,160,0.08)':'rgba(255,77,109,0.08)';
    plChart.update();
  }

  // Win rate
  if(!wChart){
    const ctx=document.getElementById('wc').getContext('2d');
    wChart=new Chart(ctx,{type:'doughnut',data:{datasets:[{data:[0,100],backgroundColor:['#00e5a0','#1a1a24'],borderWidth:0,cutout:'75%'}]},
    options:{responsive:false,plugins:{legend:{display:false},tooltip:{enabled:false}}}});
  }
  const t=d.trades;
  const wins=t.filter(x=>x.pl_pct>0).length;
  const losses=t.filter(x=>x.pl_pct<=0).length;
  const total=wins+losses;
  const rate=total>0?Math.round(wins/total*100):0;
  wChart.data.datasets[0].data=[rate,100-rate];
  wChart.update();
  document.getElementById('wrPct').textContent=total>0?rate+'%':'—';
  document.getElementById('tot').textContent=total;
  document.getElementById('wins').textContent=wins+'W';
  document.getElementById('losses').textContent=losses+'L';
  const gains=t.filter(x=>x.pl_pct>0).map(x=>x.pl_pct);
  document.getElementById('avgg').textContent=gains.length>0?'+'+(gains.reduce((a,b)=>a+b,0)/gains.length*100).toFixed(1)+'%':'—';

  // Signals
  const nf=document.getElementById('nfeed');
  if(!d.signals||d.signals.length===0){nf.innerHTML='<div class="empty">No signals this scan</div>';}
  else{nf.innerHTML=d.signals.map(s=>`
    <div class="ni">
      <div><div class="badge ${s.sentiment}">${s.sentiment.toUpperCase()}</div></div>
      <div style="flex:1">
        <div class="nh">${s.headline||s.ticker}</div>
        <div class="nm"><span class="nt">${s.ticker}</span><span>→ <b>${(s.action||'hold').toUpperCase()}</b></span><span>${s.urgency||''}</span><span>${s.reason||''}</span></div>
        <div class="sb"><div class="sf ${s.sentiment==='bearish'?'b':''}" style="width:${(s.score||0)*100}%"></div></div>
      </div>
    </div>`).join('');}

  // Positions
  const pl2=document.getElementById('plist');
  if(!d.positions||d.positions.length===0){pl2.innerHTML='<div class="empty">No open positions</div>';}
  else{pl2.innerHTML=d.positions.map(p=>{
    const plp=parseFloat(p.unrealized_plpc)*100;
    const isL=['SPY','QQQ','VTI'].includes(p.symbol);
    const prog=Math.min(Math.max((plp+4)/12*100,0),100);
    return`<div class="pi">
      <div class="pt"><div class="ptk">${p.symbol}</div><div class="ptyp ${isL?'lt':''}">${isL?'LONG':'SHORT'}</div></div>
      <div class="ps">
        <div><div class="psl">Shares</div><div class="psv">${parseFloat(p.qty).toFixed(0)}</div></div>
        <div><div class="psl">Value</div><div class="psv">$${parseFloat(p.market_value).toFixed(2)}</div></div>
        <div><div class="psl">P&L</div><div class="psv" style="color:${plp>=0?'var(--g)':'var(--l)'}">${plp>=0?'+':''}${plp.toFixed(2)}%</div></div>
      </div>
      <div class="pt2"><div class="pf2" style="width:${prog}%"></div></div>
    </div>`}).join('');}

  // Alerts
  const al=document.getElementById('alist');
  const icons={flag:'⚠️',buy:'✅',sell:'📉',profit:'🎯',info:'💡'};
  if(!d.alerts||d.alerts.length===0){al.innerHTML='<div class="empty">No alerts yet</div>';}
  else{al.innerHTML=d.alerts.map(a=>`
    <div class="ai ${a.kind}">
      <div class="aicon">${icons[a.kind]||'•'}</div>
      <div><div class="atxt">${a.msg}</div><div class="ats">${a.ts}</div></div>
    </div>`).join('');}

  // Log
  const lb=document.getElementById('logbox');
  if(d.log&&d.log.length>0){
    lb.innerHTML=d.log.map(e=>`<div class="${e.level==='gain'?'lg':e.level==='warn'?'lw':e.level==='info'?'li':e.level==='error'?'le':''}">[${e.ts}] ${e.msg}</div>`).join('');
  }
}

load();
setInterval(load, 15000);
</script>
</body>
</html>'''

@app.route('/')
def dashboard():
    return render_template_string(DASHBOARD)

@app.route('/api')
def api():
    now = time.time() * 1000
    week_cutoff  = now - 7  * 86400000
    month_cutoff = now - 30 * 86400000

    week_hist  = [e for e in equity_hist if e['ts'] >= week_cutoff]
    month_hist = [e for e in equity_hist if e['ts'] >= month_cutoff]

    cur = state['equity']
    week_pl  = (cur - week_hist[0]['val'])  if len(week_hist)  > 1 else None
    month_pl = (cur - month_hist[0]['val']) if len(month_hist) > 1 else None

    # Last 100 equity points for chart
    chart_hist = equity_hist[-100:] if equity_hist else []

    return jsonify({
        'status':      state['status'],
        'market_open': state['market_open'],
        'equity':      state['equity'],
        'buying_power':state['buying_power'],
        'today_pl':    state['today_pl'],
        'today_pl_pct':state['today_pl_pct'],
        'positions':   state['positions'],
        'signals':     state['signals'],
        'alerts':      state['alerts'],
        'log':         state['log'][:30],
        'last_scan':   state['last_scan'],
        'trades':      trades,
        'equity_hist': chart_hist,
        'week_pl':     week_pl,
        'month_pl':    month_pl,
    })

if __name__ == '__main__':
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
