let selectedSymbol = null;
let chart = null;
let candleSeries = null;
let overlaySeries = [];
let priceLines = [];

const $ = id => document.getElementById(id);
const money = value => new Intl.NumberFormat("en-US", {style:"currency", currency:"USD", maximumFractionDigits:2}).format(value || 0);
const compact = value => new Intl.NumberFormat("en-US", {notation:"compact", maximumFractionDigits:1}).format(value || 0);
const price = value => value == null ? "—" : Number(value).toLocaleString("en-US", {maximumFractionDigits: value < 1 ? 6 : 3});
const pct = value => value == null ? "—" : `${(Number(value) * 100).toFixed(2)}%`;

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
    grid: {vertLines:{color:"#f1f1f3"}, horzLines:{color:"#f1f1f3"}},
    rightPriceScale: {borderVisible:false},
    timeScale: {timeVisible:true, secondsVisible:false, borderVisible:false}
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
  $("decisionStrip").innerHTML = Object.values(decisions || {}).map(decision => `<div class="decision">
    <strong>${decision.strategy.replaceAll("_", " ")} · ${decision.action.toUpperCase()}</strong>
    <span>${(decision.reasons || []).join(" · ")}</span>
  </div>`).join("");
}

function eventText(event) {
  const payload = event.payload || {};
  if (event.event === "trade_opened") return `${payload.plan?.side || ""} ${money(payload.plan?.notional)} · target net ${money(payload.plan?.expected_net_profit)}`;
  if (event.event === "trade_closed") return `${payload.reason} · ${money(payload.netPnl)} · MAE ${money(payload.maeUsd)}`;
  if (event.event === "risk_reject") return payload.reason || "rejected";
  if (event.event === "decision") return `${payload.strategy}: ${(payload.reasons || []).join(" · ")}`;
  if (event.event === "symbol_activated") return "монета стала активной";
  return "";
}

function renderEvents(rows) {
  $("events").innerHTML = rows.slice(0, 60).map(event => `<div class="event">
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
  box.innerHTML = `<strong>${position.side.toUpperCase()} ${position.symbol}</strong>
    <span>entry ${price(position.entry)}</span><span>stop ${price(position.stop)}</span><span>target ${price(position.target)}</span>
    <span class="${pnlClass}">uPnL ${money(position.unrealized_pnl)}</span>
    <span>MAE ${money(position.mae_usd)}</span><span>MFE ${money(position.mfe_usd)}</span>`;
}

function render(data) {
  $("connection").textContent = data.botRunning ? "Бот торгует" : "Наблюдение";
  $("balance").textContent = money(data.balance);
  $("pnl").textContent = money(data.totalPnl);
  $("pnl").className = data.totalPnl >= 0 ? "positive" : "negative";
  $("positionCount").textContent = data.positions.length;
  $("availableExposure").textContent = money(data.portfolio.availableNotional);
  $("costGate").textContent = `≥ ${money(data.risk.minNetProfitUsd)} net`;
  $("sessionFile").textContent = data.sessionFile.split("/").pop();

  renderWorking(data.working);
  renderCandidates(data.candidates);
  renderStrategies(data.strategies);
  renderEvents(data.events);

  if (data.market) {
    selectedSymbol = data.market.symbol;
    const row = data.working.find(x => x.symbol === selectedSymbol);
    const position = row?.position || null;
    $("symbolTitle").textContent = data.market.symbol;
    $("symbolMeta").textContent = `1m · ${price(data.market.lastPrice)} · activity ${pct(row?.activityChange)}`;
    $("trendBadge").textContent = data.market.trend.toUpperCase();
    $("trendBadge").className = `trend ${data.market.trend}`;
    ensureChart();
    candleSeries.setData(data.market.candles);
    renderVisuals(data.market.decisions, position);
    renderBook(data.market.orderbook);
    renderDecisions(data.market.decisions);
    renderPosition(position);
  }
}

async function refresh() {
  try {
    const query = selectedSymbol ? `?symbol=${encodeURIComponent(selectedSymbol)}` : "";
    render(await api(`/api/state${query}`));
  } catch (error) {
    $("connection").textContent = "Нет связи";
  }
}

$("startBtn").onclick = async () => { await api("/api/bot/start", {method:"POST"}); refresh(); };
$("stopBtn").onclick = async () => { await api("/api/bot/stop", {method:"POST"}); refresh(); };
refresh();
setInterval(refresh, 900);
