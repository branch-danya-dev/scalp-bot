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
const price = value => value == null ? "—" : Number(value).toLocaleString("en-US", {maximumFractionDigits:precisionFor(value)});
function applyChartPrecision(value) {
  if (!candleSeries || value == null) return;
  const precision = precisionFor(value);
  candleSeries.applyOptions({priceFormat:{type:"price", precision, minMove:10 ** -precision}});
}

const REPLAY_EVENT_LABELS = {
  decision:"Решение", trade_opened:"Вход", partial_take:"Частичная фиксация",
  trade_closed:"Выход", risk_reject:"Отклонено риском", setup_blocked:"Сетап заблокирован",
  setup_consumed:"Сетап использован", setup_rearmed:"Сетап переактивирован",
  symbol_activated:"Монета активирована", symbol_deactivated:"Монета исключена",
};
const REPLAY_STRATEGY_LABELS = {
  price_action_hypothesis:"Гипотеза цены · BETA",
  trend_structure:"Трендовый откат", weak_level_rejection:"Отбой от уровня",
  orderbook_density:"Плотность в стакане", level_breakout:"Пробой уровня",
};
const REPLAY_SIDE_LABELS = {long:"ЛОНГ", short:"ШОРТ"};
function replayEventLabel(value) { return REPLAY_EVENT_LABELS[value] || String(value || "").replaceAll("_", " "); }
function replayStrategyLabel(value) { return REPLAY_STRATEGY_LABELS[value] || String(value || "—").replaceAll("_", " "); }
function replaySideLabel(value) { return REPLAY_SIDE_LABELS[String(value || "").toLowerCase()] || String(value || "—").toUpperCase(); }
function replayReason(value) {
  const text = String(value || "");
  if (!text) return "—";
  if (Object.hasOwn(EXIT_REASON_LABELS, text)) return exitReasonText(text);
  if (text.startsWith("setup expired: entry drift")) return text.replace("setup expired: entry drift", "Сетап устарел: дрейф входа");
  if (text.startsWith("setup expired after depth: entry drift")) return text.replace("setup expired after depth: entry drift", "Сетап устарел после проверки глубины: дрейф входа");
  if (text.startsWith("net at target")) return text.replace("net at target", "Net на цели").replace("after estimated trading costs", "после расчётных торговых издержек").replace("required", "требуется");
  if (text.includes("economic_gate: insufficient_net_reward_risk")) return text.replace("economic_gate: insufficient_net_reward_risk:", "Экономика: недостаточный net R:R:");
  if (text === "setup consumed") return "сетап использован";
  if (text === "rearmed") return "переактивирован";
  if (text === "deactivated") return "исключена из наблюдения";
  if (text === "partial_take") return "частичная фиксация";
  return text.replaceAll("_", " ");
}

let chart, candleSeries, bundle = null;
let candleVolume = null;
let executionDots = null;
let currentIndex = -1;
let candleMap = new Map();
let overlaySeries = [];
let priceLines = [];
let playTimer = null;
let selectedEvent = null;

async function api(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function ensureChart() {
  if (chart) return;
  chart = LightweightCharts.createChart($("replayChart"), {
    autoSize:true,
    ...chartThemeOptions(),
    localization:{
      locale:navigator.language,
      timeFormatter:time => new Date(Number(time) * 1000).toLocaleString()
    },
    rightPriceScale:{borderVisible:false},
    timeScale:{
      timeVisible:true,
      secondsVisible:true,
      borderVisible:false,
      tickMarkFormatter:time => new Date(Number(time) * 1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"})
    }
  });
  candleSeries = chart.addCandlestickSeries({
    ...chartCandleOptions()
  });
  candleVolume = addCandleVolume(chart, candleSeries, $("replayVolumeLegend"));
  executionDots = addExecutionDots(chart);
}

function resetCandleMap() {
  candleMap = new Map((bundle?.bootstrapCandles || []).map(c => [c.time, c]));
}

function applyFrame(frame) {
  if (frame?.candle) candleMap.set(frame.candle.time, frame.candle);
}

function clearOverlays() {
  for (const line of priceLines) candleSeries.removePriceLine(line);
  priceLines = [];
  for (const series of overlaySeries) chart.removeSeries(series);
  overlaySeries = [];
}

function addPriceLine(value, title, color=CHART_COLORS.level, style=2) {
  if (value == null) return;
  priceLines.push(candleSeries.createPriceLine({price:Number(value), color, lineWidth:1, lineStyle:style, axisLabelVisible:true, title}));
}

function applyVisuals(visuals) {
  for (const overlay of visuals?.overlays || []) {
    if (overlay.type === "price") addPriceLine(overlay.price, overlay.label || "уровень", CHART_COLORS.level, 2);
    if (overlay.type === "zone") {
      addPriceLine(overlay.low, `${overlay.label || "зона"} · низ`, CHART_COLORS.level, 2);
      addPriceLine(overlay.high, `${overlay.label || "зона"} · верх`, CHART_COLORS.level, 2);
    }
    if (overlay.type === "line" && overlay.points?.length >= 2) {
      const series = chart.addLineSeries({color:CHART_COLORS.trend, lineWidth:1, lineStyle:2, priceLineVisible:false, lastValueVisible:false});
      series.setData(overlay.points.map(point => ({time:point.time, value:point.value ?? point.price})));
      overlaySeries.push(series);
    }
  }
}

function applyPosition(position) {
  candleSeries.applyOptions({autoscaleInfoProvider:includeTradePrices(
    position ? [position.entry, position.stop, position.target] : []
  )});
  if (!position) return;
  addPriceLine(position.entry, "вход", CHART_COLORS.entry, 0);
  addPriceLine(position.stop, "стоп", CHART_COLORS.down, 2);
  addPriceLine(position.target, "цель", CHART_COLORS.up, 2);
}

function renderBook(book) {
  if (!book) return;
  const max = Math.max(1, ...book.bids.map(x => x[2]), ...book.asks.map(x => x[2]));
  const row = (item, kind) => `<div class="book-row ${kind}" style="--depth:${Math.max(3, item[2] / max * 100)}%">
    <span>${price(item[0])}</span><span>${Number(item[1]).toFixed(3)}</span><span>${compact(item[2])}</span>
  </div>`;
  $("replayAsks").innerHTML = [...book.asks].slice(0, 12).reverse().map(x => row(x, "ask")).join("");
  $("replayBids").innerHTML = book.bids.slice(0, 12).map(x => row(x, "bid")).join("");
  $("replayMid").textContent = book.bestBid && book.bestAsk ? price((book.bestBid + book.bestAsk) / 2) : "—";
  $("replaySpread").textContent = `спред ${(book.spreadPct * 100).toFixed(4)}%`;
}

function markerFor(event) {
  const payload = event.payload || {};
  if (event.event === "trade_opened") {
    const side = payload.plan?.side || "long";
    return executionChartMarker(payload.position?.opened_at ?? event.ts, payload.position?.entry, side);
  }
  if (event.event === "partial_take") {
    return {time:Math.floor(event.ts), position:"aboveBar", shape:"circle", color:CHART_COLORS.up, text:`ЧАСТЬ ${money(payload.netPnl)}`};
  }
  if (event.event === "trade_closed") {
    return executionChartMarker(payload.closedAt ?? event.ts, payload.exit, payload.side, true);
  }
  if (event.event === "risk_reject") {
    return {time:Math.floor(event.ts), position:"aboveBar", shape:"square", color:CHART_COLORS.level, text:"ОТКАЗ"};
  }
  return null;
}

function renderMarkers(ts) {
  const markers = (bundle?.events || []).filter(e => e.ts <= ts).map(markerFor).filter(Boolean);
  const aligned = markersOnCandles([...candleMap.values()], markers);
  candleSeries.setMarkers(aligned);
  executionDots.setMarkers(aligned);
}

function eventText(event, html = true) {
  const payload = event.payload || {};
  if (event.event === "decision") {
    const state = payload.details?.state ? ` · ${payload.details.state}` : "";
    return `${replayStrategyLabel(payload.strategy)} · ${replaySideLabel(payload.action)}${state} · ${(payload.reasons || []).map(replayReason).join(" · ")}`;
  }
  if (event.event === "trade_opened") return `${replaySideLabel(payload.plan?.side)} · вход ${price(payload.position?.entry)} · стоп ${price(payload.position?.stop)} · цель ${price(payload.position?.target)} · R:R ${Number(payload.plan?.net_reward_risk || 0).toFixed(2)}`;
  if (event.event === "partial_take") return `частичная фиксация ${money(payload.netPnl)} · остаток ${money(payload.remainingNotional)} · стоп→${price(payload.newStop)} · цель раннера ${price(payload.newTarget)}`;
  if (event.event === "trade_closed") return `${html ? exitReasonHtml(payload.reason) : exitReasonText(payload.reason)} · net ${money(payload.netPnl)} · MAE ${Number(payload.maeR || 0).toFixed(2)}R · MFE ${Number(payload.mfeR || 0).toFixed(2)}R`;
  if (event.event === "risk_reject") return replayReason(payload.reason || "Отклонено риском");
  if (event.event === "setup_blocked") return `${replayStrategyLabel(payload.strategy)} · ${replayReason(payload.reason)}`;
  if (event.event === "setup_consumed") return `${replayStrategyLabel(payload.strategy)} · сетап использован`;
  if (event.event === "setup_rearmed") return `${replayStrategyLabel(payload.strategy)} · переактивирован`;
  if (event.event === "symbol_activated") return "Монета выбрана сканером и переведена в активное наблюдение";
  if (event.event === "symbol_deactivated") return replayReason(payload.reason || "deactivated");
  return event.event;
}

function renderEvents() {
  $("replayEvents").innerHTML = (bundle?.events || []).map((event, index) => `<button class="replay-event" data-event-index="${index}">
    <time>${new Date(event.ts * 1000).toLocaleTimeString()}</time><strong>${replayEventLabel(event.event)}</strong><span>${eventText(event)}</span>
  </button>`).join("");
  document.querySelectorAll("[data-event-index]").forEach(button => {
    button.onclick = () => selectEvent(Number(button.dataset.eventIndex));
  });
}

function nearestFrameIndex(ts) {
  const frames = bundle?.frames || [];
  if (!frames.length) return 0;
  let lo = 0, hi = frames.length - 1;
  while (lo < hi) {
    const mid = Math.floor((lo + hi + 1) / 2);
    if (frames[mid].ts <= ts) lo = mid; else hi = mid - 1;
  }
  return lo;
}

function renderEventDetail(event) {
  if (!event) {
    $("replayDecision").textContent = "Двигайте временную шкалу или выберите событие бота.";
    return;
  }
  const payload = event.payload || {};
  const lines = [
    `${new Date(event.ts * 1000).toLocaleString()} · ${event.event}`,
    eventText(event, false)
  ];
  if (payload.plan) lines.push(`номинал ${money(payload.plan.notional)} · ожидаемый net ${money(payload.plan.expected_net_profit)} · net-риск ${money(payload.plan.expected_net_loss)} · R:R ${Number(payload.plan.net_reward_risk || 0).toFixed(2)} · издержки ${money(payload.plan.estimated_costs)}`);
  $("replayDecision").textContent = lines.join("\n");
}

function renderOverlayForFrame(frame) {
  clearOverlays();
  applyPosition(frame?.position);
  if (!selectedEvent) return;
  const payload = selectedEvent.payload || {};
  applyVisuals(payload.visuals || payload.decision?.visuals);
  if (payload.plan) {
    addPriceLine(payload.plan.market_entry, "вход", CHART_COLORS.entry, 0);
    addPriceLine(payload.plan.stop, "стоп", CHART_COLORS.down, 2);
    addPriceLine(payload.plan.target, "цель", CHART_COLORS.up, 2);
  }
  if (payload.watched_level != null) addPriceLine(payload.watched_level, "наблюдаемый уровень", CHART_COLORS.level, 2);
}

function seek(index) {
  const frames = bundle?.frames || [];
  if (!frames.length) return;
  index = Math.max(0, Math.min(index, frames.length - 1));
  if (index < currentIndex) {
    resetCandleMap();
    for (let i = 0; i <= index; i++) applyFrame(frames[i]);
  } else {
    for (let i = currentIndex + 1; i <= index; i++) applyFrame(frames[i]);
  }
  currentIndex = index;
  const frame = frames[index];
  applyChartPrecision(frame.lastPrice);
  candleSeries.setData([...candleMap.values()].sort((a,b) => a.time - b.time));
  candleVolume.setData([...candleMap.values()].sort((a,b) => a.time - b.time), `${bundle.symbol || ""} · 1m`);
  renderMarkers(frame.ts);
  renderBook(frame.orderbook);
  renderOverlayForFrame(frame);
  $("frameSlider").value = String(index);
  $("replayTime").textContent = new Date(frame.ts * 1000).toLocaleString();
  $("replayPrice").textContent = price(frame.lastPrice);
  $("replayTrend").textContent = (frame.trend || "—").toUpperCase();
  const flow = frame.tradeFlow || {};
  $("replayFlow").textContent = flow.tradeCount5s
    ? `${(Number(flow.imbalance5s || 0) * 100).toFixed(0)}% · x${Number(flow.acceleration || 0).toFixed(1)}`
    : "—";
  $("replayPosition").textContent = frame.position
    ? `${replaySideLabel(frame.position.side)} · ${frame.position.partial_taken ? "РАННЕР" : "ПОЛНАЯ ПОЗИЦИЯ"} · открытый PnL ${money(frame.position.unrealized_pnl)} · зафиксировано ${money(frame.position.realized_net_usd)}`
    : "Нет";
}

function selectEvent(index) {
  selectedEvent = bundle.events[index];
  const frameIndex = nearestFrameIndex(selectedEvent.ts);
  seek(frameIndex);
  renderEventDetail(selectedEvent);
  renderOverlayForFrame(bundle.frames[frameIndex]);
}

function stopPlayback() {
  if (playTimer) clearInterval(playTimer);
  playTimer = null;
  $("playBtn").textContent = "▶";
}

function togglePlayback() {
  if (playTimer) return stopPlayback();
  selectedEvent = null;
  renderEventDetail(null);
  $("playBtn").textContent = "❚❚";
  playTimer = setInterval(() => {
    const max = (bundle?.frames?.length || 1) - 1;
    if (currentIndex >= max) return stopPlayback();
    seek(currentIndex + 1);
  }, 400);
}

async function loadBundle(resetSymbols) {
  stopPlayback();
  selectedEvent = null;
  const session = $("sessionSelect").value;
  if (!session) return;
  const selected = resetSymbols ? "" : $("symbolSelect").value;
  const query = selected ? `?symbol=${encodeURIComponent(selected)}` : "";
  bundle = await api(`/api/replay/session/${encodeURIComponent(session)}${query}`);
  if (resetSymbols) {
    $("symbolSelect").innerHTML = bundle.symbols.map(symbol => `<option value="${symbol}">${symbol}</option>`).join("");
    if (bundle.symbol) $("symbolSelect").value = bundle.symbol;
  }
  ensureChart();
  currentIndex = -1;
  resetCandleMap();
  candleSeries.setData([...candleMap.values()].sort((a,b) => a.time - b.time));
  candleVolume.setData([...candleMap.values()].sort((a,b) => a.time - b.time), `${bundle.symbol || ""} · 1m`);
  candleSeries.setMarkers([]);
  executionDots.setMarkers([]);
  clearOverlays();
  renderEvents();
  renderEventDetail(null);
  $("replayTitle").textContent = bundle.symbol || "График";
  $("frameSlider").max = String(Math.max(0, bundle.frames.length - 1));
  $("frameSlider").value = "0";
  if (bundle.frames.length) seek(0);
  chart.timeScale().fitContent();
}

async function init() {
  const data = await api("/api/replay/sessions");
  $("sessionSelect").innerHTML = data.sessions.map(row => `<option value="${row.name}">${row.name}</option>`).join("");
  if (data.sessions.length) await loadBundle(true);
}

$("sessionSelect").onchange = () => loadBundle(true);
$("symbolSelect").onchange = () => loadBundle(false);
$("frameSlider").oninput = event => { selectedEvent = null; renderEventDetail(null); seek(Number(event.target.value)); };
$("playBtn").onclick = togglePlayback;
init();
