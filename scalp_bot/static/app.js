let selectedSymbol = null;
let chart = null;
let candleSeries = null;
let overlaySeries = [];
let priceLines = [];

const $ = id => document.getElementById(id);
const money = value => new Intl.NumberFormat("en-US", {style:"currency", currency:"USD", maximumFractionDigits:2}).format(value || 0);
const compact = value => new Intl.NumberFormat("en-US", {notation:"compact", maximumFractionDigits:1}).format(value || 0);
function precisionFor(value) {
  const abs = Math.abs(Number(value) || 0);
  if (abs >= 1000) return 2;
  if (abs >= 100) return 3;
  if (abs >= 1) return 4;
  if (abs >= 0.1) return 5;
  if (abs >= 0.01) return 6;
  if (abs >= 0.001) return 7;
  return 8;
}
const price = value => {
  if (value == null) return "—";
  const precision = precisionFor(value);
  return Number(value).toLocaleString("en-US", {maximumFractionDigits: precision});
};
function applyChartPrecision(value) {
  if (!candleSeries || value == null) return;
  const precision = precisionFor(value);
  candleSeries.applyOptions({priceFormat:{type:"price", precision, minMove:10 ** -precision}});
}
const pct = value => value == null ? "—" : `${(Number(value) * 100).toFixed(2)}%`;
function duration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = String(Math.floor(total / 3600)).padStart(2, "0");
  const m = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const s = String(total % 60).padStart(2, "0");
  return `${h}:${m}:${s}`;
}

async function api(path, options={}) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function ensureChart() {
  if (chart) return;
  chart = LightweightCharts.createChart($("chart"), {
    autoSize: true,
    layout: {background:{color:"transparent"}, textColor:"#6e6e73"},
    localization: {
      locale: navigator.language,
      timeFormatter: time => new Date(Number(time) * 1000).toLocaleString()
    },
    grid: {vertLines:{color:"#f1f1f3"}, horzLines:{color:"#f1f1f3"}},
    rightPriceScale: {borderVisible:false},
    timeScale: {
      timeVisible:true,
      secondsVisible:false,
      borderVisible:false,
      tickMarkFormatter: time => new Date(Number(time) * 1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"})
    }
  });
  candleSeries = chart.addCandlestickSeries({
    upColor:"#34c759", downColor:"#ff453a", borderVisible:false,
    wickUpColor:"#34c759", wickDownColor:"#ff453a"
  });
}

function clearOverlays() {
  if (!chart || !candleSeries) return;
  for (const line of priceLines) candleSeries.removePriceLine(line);
  priceLines = [];
  for (const series of overlaySeries) chart.removeSeries(series);
  overlaySeries = [];
}

function addPriceLine(value, title, color="#8e8e93", style=2) {
  if (!candleSeries || value == null) return;
  priceLines.push(candleSeries.createPriceLine({price:Number(value), color, lineWidth:1, lineStyle:style, axisLabelVisible:true, title}));
}

function renderVisuals(decisions, position) {
  clearOverlays();
  Object.values(decisions || {}).forEach(decision => {
    const overlays = decision.visuals?.overlays || [];
    overlays.forEach(overlay => {
      if (overlay.type === "price") {
        addPriceLine(overlay.price, overlay.label || "level", "#8e8e93", 2);
      } else if (overlay.type === "zone") {
        addPriceLine(overlay.low, `${overlay.label || "zone"} low`, "#8e8e93", 2);
        addPriceLine(overlay.high, `${overlay.label || "zone"} high`, "#8e8e93", 2);
      } else if (overlay.type === "line" && overlay.points?.length >= 2) {
        const series = chart.addLineSeries({color:"#7c7c80", lineWidth:1, lineStyle:2, priceLineVisible:false, lastValueVisible:false});
        series.setData(overlay.points);
        overlaySeries.push(series);
      }
    });
  });
  if (position) {
    addPriceLine(position.entry, "entry", "#007aff", 0);
    addPriceLine(position.stop, "stop", "#ff3b30", 2);
    addPriceLine(position.target, "target", "#34c759", 2);
  }
}

function renderWorking(rows) {
  if (!selectedSymbol && rows.length) selectedSymbol = rows[0].symbol;
  $("workCount").textContent = rows.length;
  $("workingList").innerHTML = rows.map(row => {
    const active = row.symbol === selectedSymbol ? "active" : "";
    const moveClass = (row.activityChange || 0) >= 0 ? "up" : "down";
    const position = row.position ? `<span class="pill">${row.position.side.toUpperCase()}</span>` : `<span>${row.trend.toUpperCase()}</span>`;
    return `<button class="symbol-row ${active}" data-symbol="${row.symbol}">
      <strong>#${row.activityRank || "—"} ${row.symbol.replace("USDT", "")}</strong><span>${price(row.lastPrice)}</span>
      ${position}<span class="${moveClass}">${pct(row.activityChange)}</span>
    </button>`;
  }).join("");
  document.querySelectorAll("[data-symbol]").forEach(button => {
    button.onclick = () => { selectedSymbol = button.dataset.symbol; refresh(); };
  });
}

function renderCandidates(rows) {
  $("candidateList").innerHTML = rows.slice(0, 30).map(row => {
    const moveClass = row.activity_change >= 0 ? "up" : "down";
    return `<div class="candidate-row">
      <span>#${row.activity_rank || "—"} ${row.symbol}</span>
      <span class="${moveClass}">${pct(row.activity_change)}</span>
      <span>${compact(row.turnover_24h)}</span>
    </div>`;
  }).join("");
}

function renderBook(book) {
  if (!book) return;
  const max = Math.max(1, ...book.bids.map(x => x[2]), ...book.asks.map(x => x[2]));
  const row = (item, kind) => `<div class="book-row ${kind}" style="--depth:${Math.max(3, item[2] / max * 100)}%">
    <span>${price(item[0])}</span><span>${Number(item[1]).toFixed(3)}</span><span>${compact(item[2])}</span>
  </div>`;
  $("asks").innerHTML = [...book.asks].slice(0, 10).reverse().map(x => row(x, "ask")).join("");
  $("bids").innerHTML = book.bids.slice(0, 10).map(x => row(x, "bid")).join("");
  $("midPrice").textContent = book.bestBid && book.bestAsk ? price((book.bestBid + book.bestAsk) / 2) : "—";
  $("spread").textContent = `spread ${(book.spreadPct * 100).toFixed(4)}%`;
}

function renderStrategies(rows) {
  $("strategyList").innerHTML = rows.map(row => `<div class="strategy-row">
    <span>${row.label}</span><button class="switch ${row.enabled ? "on" : ""}" data-strategy="${row.key}" data-enabled="${row.enabled}"></button>
  </div>`).join("");
  document.querySelectorAll("[data-strategy]").forEach(button => {
    button.onclick = async () => {
      await api(`/api/strategies/${button.dataset.strategy}`, {
        method:"POST", headers:{"content-type":"application/json"},
        body:JSON.stringify({enabled: button.dataset.enabled !== "true"})
      });
      refresh();
    };
  });
}

function renderDecisions(decisions) {
  $("decisionStrip").innerHTML = Object.values(decisions || {}).map(decision => {
    const state = decision.details?.state ? ` · ${decision.details.state.toUpperCase()}` : "";
    return `<div class="decision">
      <strong>${decision.strategy.replaceAll("_", " ")} · ${decision.action.toUpperCase()}${state}</strong>
      <span>${(decision.reasons || []).join(" · ")}</span>
    </div>`;
  }).join("");
}

function eventText(event) {
  const payload = event.payload || {};
  if (event.event === "trade_opened") return `${payload.plan?.side || ""} ${money(payload.plan?.notional)} · net target ${money(payload.plan?.expected_net_profit)} · RR ${Number(payload.plan?.net_reward_risk || 0).toFixed(2)}`;
  if (event.event === "partial_take") return `partial ${money(payload.netPnl)} · осталось ${money(payload.remainingNotional)} · stop→${price(payload.newStop)}`;
  if (event.event === "trade_closed") return `${payload.reason} · ${money(payload.netPnl)} · MAE ${money(payload.maeUsd)} · MFE ${money(payload.mfeUsd)}`;
  if (event.event === "risk_reject") return payload.reason || "rejected";
  if (event.event === "setup_blocked") return `${payload.strategy}: ${payload.reason}`;
  if (event.event === "setup_consumed") return `${payload.strategy}: setup consumed`;
  if (event.event === "setup_rearmed") return `${payload.strategy}: rearmed`;
  if (event.event === "decision") return `${payload.strategy}: ${(payload.reasons || []).join(" · ")}`;
  if (event.event === "symbol_activated") return "монета стала активной";
  if (event.event === "symbol_deactivated") return payload.reason || "deactivated";
  if (event.event === "run_summary") return `${payload.reason} · elapsed ${duration(payload.elapsedSeconds)} · PnL ${money(payload.realizedPnl)} · trades ${payload.closedTrades}`;
  if (event.event === "bot_stopped") return payload.reason || "stopped";
  return "";
}

function compactEvents(rows) {
  const result = [];
  let previousDecisionKey = null;
  for (const event of rows) {
    if (event.event !== "decision") {
      previousDecisionKey = null;
      result.push(event);
      continue;
    }
    const payload = event.payload || {};
    const key = [event.symbol, payload.strategy, payload.action, ...(payload.reasons || [])].join("|");
    if (key === previousDecisionKey) continue;
    previousDecisionKey = key;
    result.push(event);
  }
  return result;
}

function renderEvents(rows) {
  $("events").innerHTML = compactEvents(rows).slice(0, 60).map(event => `<div class="event">
    <time>${new Date(event.ts * 1000).toLocaleTimeString()}</time>
    <span class="type">${event.event}</span>
    <span class="text">${event.symbol || ""} ${eventText(event)}</span>
  </div>`).join("");
}

function renderPosition(position) {
  const box = $("positionCard");
  if (!position) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.classList.remove("hidden");
  const pnlClass = position.unrealized_pnl >= 0 ? "positive" : "negative";
  const phase = position.partial_taken ? "RUNNER" : "INITIAL";
  box.innerHTML = `<strong>${position.side.toUpperCase()} ${position.symbol} · ${phase}</strong>
    <span>remaining ${money(position.notional)}</span>
    <span>entry ${price(position.entry)}</span><span>stop ${price(position.stop)}</span><span>target ${price(position.target)}</span>
    <span class="${pnlClass}">uPnL ${money(position.unrealized_pnl)}</span>
    <span>locked ${money(position.realized_net_usd)}</span>
    <span>MAE ${Number(position.mae_r || 0).toFixed(2)}R</span><span>MFE ${Number(position.mfe_r || 0).toFixed(2)}R</span>`;
}

function renderTrades(rows) {
  const root = $("closedTrades");
  if (!rows?.length) {
    root.innerHTML = '<div class="empty-row">Закрытых paper-сделок пока нет.</div>';
    return;
  }
  root.innerHTML = rows.slice().reverse().map(trade => {
    const netClass = trade.netPnl >= 0 ? "positive" : "negative";
    return `<div class="trade-row">
      <strong>${trade.symbol}</strong>
      <span>${trade.side.toUpperCase()}</span>
      <span>${price(trade.entry)} → ${price(trade.exit)}</span>
      <span>${money(trade.fees)}</span>
      <span>${money(trade.maeUsd)}</span>
      <span>${money(trade.mfeUsd)}</span>
      <span>${trade.reason}</span>
      <strong class="${netClass}">${money(trade.netPnl)}</strong>
    </div>`;
  }).join("");
}

function render(data) {
  const status = $("connection");
  const lossCap = data.risk?.sessionLossLimitEnabled ? "LOSS CAP ON" : "RESEARCH · LOSS CAP OFF";
  status.textContent = data.botRunning
    ? `PAPER TRADING ON · ${lossCap}`
    : `PAPER OFF · ${lossCap}`;
  status.className = data.botRunning ? "live trading-on" : "live observing";
  $("startBtn").disabled = data.botRunning;
  $("stopBtn").disabled = !data.botRunning;

  const openPnl = data.positions.reduce((sum, position) => sum + Number(position.unrealized_pnl || 0), 0);
  const totalNet = Number(data.totalPnl || 0) + openPnl;

  $("balance").textContent = money(data.balance);
  $("realizedPnl").textContent = money(data.totalPnl);
  $("realizedPnl").className = data.totalPnl >= 0 ? "positive" : "negative";
  $("openPnl").textContent = money(openPnl);
  $("openPnl").className = openPnl >= 0 ? "positive" : "negative";
  $("netPnl").textContent = money(totalNet);
  $("netPnl").className = totalNet >= 0 ? "positive" : "negative";
  $("positionCount").textContent = data.positions.length;
  const perPositionCap = Number(data.balance || 0) * Number(data.risk.maxLeverage || 0) * Number(data.risk.maxPositionExposureFraction || 0);
  $("availableExposure").textContent = `${money(data.portfolio.availableNotional)} · ${money(perPositionCap)}/pos`;
  const rrGate = data.risk.enforceNetRewardRiskGate ? `RR≥${Number(data.risk.minNetRewardRisk || 0).toFixed(2)}` : `RR monitor ${Number(data.risk.minNetRewardRisk || 0).toFixed(2)}`;
  $("costGate").textContent = `≥ ${money(data.risk.minNetProfitUsd)} net · ${rrGate}`;
  $("runTimer").textContent = data.botRunning
    ? duration(data.run?.remainingSeconds)
    : duration(data.run?.configuredDurationSeconds);
  $("sessionFile").textContent = data.sessionFile.split("/").pop();

  renderWorking(data.working);
  renderCandidates(data.candidates);
  renderStrategies(data.strategies);
  renderEvents(data.events);
  renderTrades(data.closedTrades);

  if (data.market) {
    selectedSymbol = data.market.symbol;
    const row = data.working.find(x => x.symbol === selectedSymbol);
    const position = row?.position || null;
    const book = data.market.orderbook;
    const mid = book?.bestBid && book?.bestAsk ? (book.bestBid + book.bestAsk) / 2 : null;
    const gapBps = mid ? (data.market.lastPrice - mid) / mid * 10000 : null;
    const gapText = gapBps == null ? "" : ` · last↔book ${gapBps >= 0 ? "+" : ""}${gapBps.toFixed(1)} bps`;

    $("symbolTitle").textContent = data.market.symbol;
    const flow = data.market.tradeFlow || {};
    const flowText = flow.tradeCount5s
      ? ` · flow5s ${(Number(flow.imbalance5s || 0) * 100).toFixed(0)}% · speed x${Number(flow.acceleration || 0).toFixed(1)}`
      : "";
    $("symbolMeta").textContent = `1m · last ${price(data.market.lastPrice)}${gapText} · activity ${pct(row?.activityChange)}${flowText}`;
    $("trendBadge").textContent = data.market.trend.toUpperCase();
    $("trendBadge").className = `trend ${data.market.trend}`;
    ensureChart();
    applyChartPrecision(data.market.lastPrice);
    candleSeries.setData(data.market.candles);
    renderVisuals(data.market.decisions, position);
    renderBook(book);
    renderDecisions(data.market.decisions);
    renderPosition(position);
  }
}

async function refresh() {
  try {
    const query = selectedSymbol ? `?symbol=${encodeURIComponent(selectedSymbol)}` : "";
    render(await api(`/api/state${query}`));
  } catch (error) {
    const status = $("connection");
    status.textContent = "Нет связи";
    status.className = "live disconnected";
  }
}

$("startBtn").onclick = async () => { await api("/api/bot/start", {method:"POST"}); refresh(); };
$("stopBtn").onclick = async () => { await api("/api/bot/stop", {method:"POST"}); refresh(); };
refresh();
setInterval(refresh, 900);
