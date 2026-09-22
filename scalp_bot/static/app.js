let selectedSymbol = null;
let chart = null;
let candleSeries = null;
let overlaySeries = [];
let priceLines = [];
let tradeReviewSummaries = [];
let lastClosedTradeCount = -1;
const reviewCharts = new Map();
let selectedChartTimeframe = "1m";
let levelFilter = "active";
let showTrendlines = true;
let lastMarketForChart = null;
let lastPositionForChart = null;

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

function addPriceLine(value, title, color="#8e8e93", style=2, width=1, axisLabelVisible=true) {
  if (!candleSeries || value == null) return;
  priceLines.push(candleSeries.createPriceLine({
    price:Number(value), color, lineWidth:width, lineStyle:style,
    axisLabelVisible, title
  }));
}

function levelTimeframe(level) {
  const sources = level.sources || [];
  if (sources.includes("1h") || level.timeframe === "1h") return "1h";
  if (sources.includes("15m") || level.timeframe === "15m") return "15m";
  if (sources.includes("5m") || level.timeframe === "5m") return "5m";
  if (level.timeframe === "1D" || String(level.kind || "").includes("day_")) return "1D";
  return "1m";
}

function drawStructuralLevels(structure) {
  const levels = structure?.levels || [];
  if (levelFilter === "active") return;
  const filtered = levels.filter(level => {
    if (levelFilter === "htf") {
      return ["15m", "1h", "1D"].includes(levelTimeframe(level));
    }
    return true;
  }).slice(0, levelFilter === "all" ? 14 : 10);

  filtered.forEach(level => {
    const tf = levelTimeframe(level);
    const htf = ["1h", "1D"].includes(tf);
    const mid = level.center ?? ((Number(level.low) + Number(level.high)) / 2);
    const title = `${level.kind} · ${tf}`;
    addPriceLine(
      mid,
      title,
      htf ? "#af52de" : tf === "15m" ? "#5856d6" : "#b0b0b5",
      htf ? 0 : 2,
      htf ? 2 : 1,
      htf
    );
  });
}

function drawActiveDecisionObjects(decisions) {
  Object.values(decisions || {}).forEach(decision => {
    const trace = decision.trace || {};
    const object = trace.object || {};
    const label = `${decision.strategy.replaceAll("_", " ")} · ${trace.state || decision.details?.state || ""}`;
    if (object.low != null && object.high != null) {
      addPriceLine(object.low, label + " low", "#007aff", 0, 2, true);
      addPriceLine(object.high, label + " high", "#007aff", 0, 2, true);
    } else if (object.price != null) {
      addPriceLine(object.price, label, "#007aff", 0, 2, true);
    } else if (decision.watched_level != null) {
      addPriceLine(decision.watched_level, label, "#007aff", 0, 2, true);
    }
    const target = decision.details?.liquidityTarget?.price;
    if (target != null) addPriceLine(target, "liquidity target", "#34c759", 2, 1, true);
  });
}

function renderVisuals(decisions, position, structure) {
  clearOverlays();
  drawStructuralLevels(structure);
  drawActiveDecisionObjects(decisions);

  if (showTrendlines) {
    (structure?.trendlines || []).slice(0, levelFilter === "all" ? 4 : 2).forEach(line => {
      const series = chart.addLineSeries({
        color:"#98989d", lineWidth:2, lineStyle:2,
        priceLineVisible:false, lastValueVisible:false
      });
      series.setData([
        {time: Math.floor(line.start_ms / 1000), price: line.start_price},
        {time: Math.floor(line.end_ms / 1000), price: line.end_price},
      ]);
      overlaySeries.push(series);
    });
  }

  if (position) {
    addPriceLine(position.entry, "ENTRY", "#007aff", 0, 2, true);
    addPriceLine(position.stop, "STOP", "#ff3b30", 0, 2, true);
    addPriceLine(position.target, "TARGET", "#34c759", 0, 2, true);
  }
}

function renderMarketChart(market, position) {
  if (!market) return;
  ensureChart();
  lastMarketForChart = market;
  lastPositionForChart = position;
  const rows = market.chartSeries?.[selectedChartTimeframe] || market.candles || [];
  const normalized = rows.map(row => ({
    time:Number(row.time),
    open:Number(row.open),
    high:Number(row.high),
    low:Number(row.low),
    close:Number(row.close),
  })).filter(row => Number.isFinite(row.time) && Number.isFinite(row.close));
  if (normalized.length) {
    applyChartPrecision(normalized[normalized.length - 1].close);
    candleSeries.setData(normalized);
  } else {
    candleSeries.setData([]);
  }
  renderVisuals(market.decisions, position, market.structure);
}

function bindChartControls() {
  document.querySelectorAll("[data-timeframe]").forEach(button => {
    button.onclick = () => {
      selectedChartTimeframe = button.dataset.timeframe;
      document.querySelectorAll("[data-timeframe]").forEach(row =>
        row.classList.toggle("active", row.dataset.timeframe === selectedChartTimeframe)
      );
      renderMarketChart(lastMarketForChart, lastPositionForChart);
    };
  });
  document.querySelectorAll("[data-level-filter]").forEach(button => {
    button.onclick = () => {
      levelFilter = button.dataset.levelFilter;
      document.querySelectorAll("[data-level-filter]").forEach(row =>
        row.classList.toggle("active", row.dataset.levelFilter === levelFilter)
      );
      renderMarketChart(lastMarketForChart, lastPositionForChart);
    };
  });
  const toggle = $("trendlineToggle");
  if (toggle) {
    toggle.onclick = () => {
      showTrendlines = !showTrendlines;
      toggle.classList.toggle("active", showTrendlines);
      renderMarketChart(lastMarketForChart, lastPositionForChart);
    };
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
  if (event.event === "trade_opened") return `${payload.plan?.side || ""} ${money(payload.plan?.notional)} · net@target ${money(payload.plan?.net_at_target ?? payload.plan?.expected_net_profit)} · quality ${Number(payload.opportunityQuality ?? 0).toFixed(2)}`;
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
  if (event.event === "startup_scan_error") return payload.error || "startup scanner failed";
  if (event.event === "scanner_error") return payload.error || "scanner failed";
  if (event.event === "symbol_bootstrap_error") return payload.error || "symbol bootstrap failed";
  if (event.event === "context_error") return payload.error || "context refresh failed";
  if (event.event === "strategy_error") return payload.error || "strategy failed";
  return payload.error || "";
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

function reviewSummaryFor(trade) {
  return tradeReviewSummaries.find(review =>
    review.symbol === trade.symbol
    && review.setupId === trade.setupId
    && review.strategy === trade.strategy
  ) || null;
}

function timelineText(row) {
  const payload = row.payload || {};
  const trace = payload.trace || {};
  if (row.event === "decision") {
    const object = trace.object || {};
    const objectText = object.price != null
      ? ` @ ${price(object.price)}`
      : object.low != null && object.high != null
        ? ` ${price(object.low)}–${price(object.high)}`
        : "";
    const waiting = (trace.waitingFor || []).slice(0, 2).join(" · ");
    return `${trace.strategy || payload.strategy || ""} · ${trace.state || payload.details?.state || ""}${objectText}${waiting ? " · ждём: " + waiting : ""}`;
  }
  if (row.event === "trade_opened") return "Позиция открыта";
  if (row.event === "partial_take") return `Partial · ${money(payload.netPnl)}`;
  if (row.event === "trade_closed") return `${payload.reason || "closed"} · ${money(payload.netPnl)}`;
  if (row.event === "risk_reject") return `Risk reject · ${payload.reason || ""}`;
  if (row.event === "setup_blocked") return `Setup blocked · ${payload.reason || ""}`;
  return row.event.replaceAll("_", " ");
}

function destroyReviewChart(reviewId) {
  const chart = reviewCharts.get(reviewId);
  if (chart) chart.remove();
  reviewCharts.delete(reviewId);
}

function renderTradeReviewDetail(reviewId, review) {
  const root = document.querySelector(`[data-review-detail="${CSS.escape(reviewId)}"]`);
  if (!root) return;
  destroyReviewChart(reviewId);
  const summary = review.summary || {};
  const details = review.strategyDetails || {};
  root.innerHTML = `
    <div class="review-grid">
      <div class="review-chart" data-review-chart="${reviewId}"></div>
      <div class="review-timeline">
        <div class="review-subtitle">Decision timeline</div>
        <div class="review-events">
          ${(review.timeline || []).map(row => `<div class="review-event">
            <time>${new Date(row.ts * 1000).toLocaleTimeString()}</time>
            <span class="review-event-type">${row.event}</span>
            <span>${timelineText(row)}</span>
          </div>`).join("") || '<div class="empty-row">Timeline отсутствует.</div>'}
        </div>
      </div>
    </div>
    <div class="review-context">
      <div><b>Strategy:</b> ${summary.strategy || "—"}</div>
      <div><b>Setup:</b> ${summary.setupId || "—"}</div>
      <div><b>Target source:</b> ${details.targetSource || "—"}</div>
      <div><b>Exit:</b> ${summary.reason || "—"}</div>
    </div>`;

  const chartRoot = root.querySelector("[data-review-chart]");
  if (!chartRoot || !window.LightweightCharts) return;
  const mini = LightweightCharts.createChart(chartRoot, {
    autoSize:true,
    height:300,
    layout:{background:{color:"transparent"}, textColor:"#6e6e73"},
    grid:{vertLines:{color:"#f1f1f3"}, horzLines:{color:"#f1f1f3"}},
    rightPriceScale:{borderVisible:false},
    timeScale:{timeVisible:true, secondsVisible:false, borderVisible:false},
  });
  const series = mini.addCandlestickSeries({
    upColor:"#34c759", downColor:"#ff453a", borderVisible:false,
    wickUpColor:"#34c759", wickDownColor:"#ff453a",
  });
  series.setData((review.candles || []).map(candle => ({
    time:Number(candle.time),
    open:Number(candle.open),
    high:Number(candle.high),
    low:Number(candle.low),
    close:Number(candle.close),
  })));
  const line = (value, title, color) => {
    if (value == null) return;
    series.createPriceLine({
      price:Number(value), title, color, lineWidth:2,
      lineStyle:0, axisLabelVisible:true,
    });
  };
  line(summary.entry, "ENTRY", "#007aff");
  line(summary.initialStop, "STOP", "#ff3b30");
  line(summary.target, "TARGET", "#34c759");
  line(summary.exit, "EXIT", "#af52de");
  mini.timeScale().fitContent();
  reviewCharts.set(reviewId, mini);
}

async function openTradeReview(reviewId) {
  const detail = document.querySelector(`[data-review-detail="${CSS.escape(reviewId)}"]`);
  if (!detail) return;
  if (!detail.classList.contains("hidden")) {
    detail.classList.add("hidden");
    destroyReviewChart(reviewId);
    return;
  }
  detail.classList.remove("hidden");
  detail.innerHTML = '<div class="empty-row">Загрузка разбора сделки…</div>';
  try {
    const review = await api(`/api/reviews/trades/${encodeURIComponent(reviewId)}`);
    renderTradeReviewDetail(reviewId, review);
  } catch (error) {
    detail.innerHTML = `<div class="review-error">${String(error.message || error)}</div>`;
  }
}

function renderTrades(rows) {
  const root = $("closedTrades");
  if (!rows?.length) {
    root.innerHTML = '<div class="empty-row">Закрытых paper-сделок пока нет.</div>';
    return;
  }
  root.innerHTML = rows.slice().reverse().map(trade => {
    const netClass = trade.netPnl >= 0 ? "positive" : "negative";
    const review = reviewSummaryFor(trade);
    const reviewButton = review
      ? `<button class="button secondary review-open" data-open-review="${review.reviewId}">Разбор сделки</button>`
      : '<span class="review-pending">Review формируется</span>';
    return `<article class="trade-card">
      <div class="trade-card-head">
        <div>
          <strong>${trade.symbol} · ${trade.side.toUpperCase()}</strong>
          <span>${trade.strategy || "strategy"}</span>
        </div>
        <strong class="trade-card-net ${netClass}">${money(trade.netPnl)}</strong>
      </div>
      <div class="trade-card-metrics">
        <span><small>Entry → Exit</small>${price(trade.entry)} → ${price(trade.exit)}</span>
        <span><small>MAE</small>${money(trade.maeUsd)} · ${Number(trade.maeR || 0).toFixed(2)}R</span>
        <span><small>MFE</small>${money(trade.mfeUsd)} · ${Number(trade.mfeR || 0).toFixed(2)}R</span>
        <span><small>Fees</small>${money(trade.fees)}</span>
        <span><small>Exit reason</small>${trade.reason || "—"}</span>
      </div>
      <div class="trade-card-actions">${reviewButton}</div>
      ${review ? `<div class="trade-review-detail hidden" data-review-detail="${review.reviewId}"></div>` : ""}
    </article>`;
  }).join("");
  document.querySelectorAll("[data-open-review]").forEach(button => {
    button.onclick = () => openTradeReview(button.dataset.openReview);
  });
}

function render(data) {
  const status = $("connection");
  const lossCap = data.risk?.sessionLossLimitEnabled ? "LOSS CAP ON" : "RESEARCH · LOSS CAP OFF";
  const marketHealth = data.marketHealth || {};
  const marketReady = Boolean(marketHealth.ready);
  status.textContent = data.botRunning
    ? `PAPER TRADING ON · ${lossCap}`
    : marketReady
      ? `PAPER READY · ${lossCap}`
      : marketHealth.scannerError
        ? "MARKET DATA ERROR"
        : "ОЖИДАНИЕ РЫНКА";
  status.className = data.botRunning
    ? "live trading-on"
    : marketReady
      ? "live observing"
      : "live disconnected";
  $("startBtn").disabled = data.botRunning || !marketReady;
  $("stopBtn").disabled = !data.botRunning;

  const marketAlert = $("marketAlert");
  if (!marketReady) {
    marketAlert.classList.remove("hidden");
    marketAlert.textContent = marketHealth.reason
      ? `Рыночные данные не готовы: ${marketHealth.reason}`
      : "Рыночные данные ещё инициализируются.";
  } else {
    marketAlert.classList.add("hidden");
    marketAlert.textContent = "";
  }

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
  const balance = Number(data.balance || 0);
  const positionLeverageCap = balance * Number(data.risk.maxPositionLeverage || 0);
  const positionShareCap = balance
    * Number(data.risk.maxPortfolioLeverage || 0)
    * Number(data.risk.maxPositionExposureFraction || 0);
  const perPositionCap = Math.min(positionLeverageCap, positionShareCap);
  $("availableExposure").textContent = money(data.portfolio.availableNotional) + " · " + money(perPositionCap) + "/pos";
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

  if (data.closedTrades.length !== lastClosedTradeCount) {
    lastClosedTradeCount = data.closedTrades.length;
    api("/api/reviews/trades")
      .then(payload => {
        tradeReviewSummaries = payload.reviews || [];
        renderTrades(data.closedTrades);
      })
      .catch(() => {});
  }

  if (!data.market) {
    $("symbolTitle").textContent = "—";
    $("symbolMeta").textContent = data.marketHealth?.reason
      ? `Рынок недоступен: ${data.marketHealth.reason}`
      : "Ожидание рыночных данных";
    $("trendBadge").textContent = "—";
    $("trendBadge").className = "trend flat";
    $("asks").innerHTML = "";
    $("bids").innerHTML = "";
    $("midPrice").textContent = "—";
    $("spread").textContent = "—";
    $("decisionStrip").innerHTML = "";
    renderPosition(null);
  }

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
    const profile = data.market.activityProfile || {};
    const corr = profile.correlation_1h_btc == null
      ? "corr1h n/a"
      : `corr1h BTC ${(Number(profile.correlation_1h_btc) * 100).toFixed(0)}%`;
    const trades24h = profile.trade_count_24h == null
      ? "trades24h n/a"
      : `trades24h ${compact(profile.trade_count_24h)}`;
    $("symbolMeta").textContent = `1m · last ${price(data.market.lastPrice)}${gapText} · 24h ${pct(profile.change_24h)} · vol ${compact(profile.turnover_24h)} · ${corr} · ${trades24h} · score ${Number(profile.activity_score || 0).toFixed(0)}${flowText}`;
    $("trendBadge").textContent = data.market.trend.toUpperCase();
    $("trendBadge").className = `trend ${data.market.trend}`;
    renderMarketChart(data.market, position);
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

$("startBtn").onclick = async () => {
  try {
    await api("/api/bot/start", {method:"POST"});
  } catch (error) {
    const alert = $("marketAlert");
    alert.classList.remove("hidden");
    alert.textContent = String(error.message || error);
  }
  refresh();
};
$("stopBtn").onclick = async () => { await api("/api/bot/stop", {method:"POST"}); refresh(); };
bindChartControls();
refresh();
setInterval(refresh, 900);
