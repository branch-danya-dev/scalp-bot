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

let chart, candleSeries, bundle = null;
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
    layout:{background:{color:"transparent"}, textColor:"#6e6e73"},
    localization:{
      locale:navigator.language,
      timeFormatter:time => new Date(Number(time) * 1000).toLocaleString()
    },
    grid:{vertLines:{color:"#f1f1f3"}, horzLines:{color:"#f1f1f3"}},
    rightPriceScale:{borderVisible:false},
    timeScale:{
      timeVisible:true,
      secondsVisible:true,
      borderVisible:false,
      tickMarkFormatter:time => new Date(Number(time) * 1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"})
    }
  });
  candleSeries = chart.addCandlestickSeries({
    upColor:"#34c759", downColor:"#ff453a", borderVisible:false,
    wickUpColor:"#34c759", wickDownColor:"#ff453a"
  });
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

function addPriceLine(value, title, color="#8e8e93", style=2) {
  if (value == null) return;
  priceLines.push(candleSeries.createPriceLine({price:Number(value), color, lineWidth:1, lineStyle:style, axisLabelVisible:true, title}));
}

function applyVisuals(visuals) {
  for (const overlay of visuals?.overlays || []) {
    if (overlay.type === "price") addPriceLine(overlay.price, overlay.label || "level", "#8e8e93", 2);
    if (overlay.type === "zone") {
      addPriceLine(overlay.low, `${overlay.label || "zone"} low`, "#8e8e93", 2);
      addPriceLine(overlay.high, `${overlay.label || "zone"} high`, "#8e8e93", 2);
    }
    if (overlay.type === "line" && overlay.points?.length >= 2) {
      const series = chart.addLineSeries({color:"#7c7c80", lineWidth:1, lineStyle:2, priceLineVisible:false, lastValueVisible:false});
      series.setData(overlay.points);
      overlaySeries.push(series);
    }
  }
}

function applyPosition(position) {
  if (!position) return;
  addPriceLine(position.entry, "entry", "#007aff", 0);
  addPriceLine(position.stop, "stop", "#ff3b30", 2);
  addPriceLine(position.target, "target", "#34c759", 2);
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
  $("replaySpread").textContent = `spread ${(book.spreadPct * 100).toFixed(4)}%`;
}

function markerFor(event) {
  const payload = event.payload || {};
  if (event.event === "trade_opened") {
    const side = payload.plan?.side || "long";
    return {time:Math.floor(event.ts), position:side === "long" ? "belowBar" : "aboveBar", shape:side === "long" ? "arrowUp" : "arrowDown", color:side === "long" ? "#34c759" : "#ff453a", text:`ENTRY ${side.toUpperCase()}`};
  }
  if (event.event === "trade_closed") {
    return {time:Math.floor(event.ts), position:"aboveBar", shape:"circle", color:"#007aff", text:`EXIT ${money(payload.netPnl)}`};
  }
  if (event.event === "risk_reject") {
    return {time:Math.floor(event.ts), position:"aboveBar", shape:"square", color:"#8e8e93", text:"REJECT"};
  }
  return null;
}

function renderMarkers(ts) {
  const markers = (bundle?.events || []).filter(e => e.ts <= ts).map(markerFor).filter(Boolean).sort((a,b) => a.time - b.time);
  candleSeries.setMarkers(markers);
}

function eventText(event) {
  const payload = event.payload || {};
  if (event.event === "decision") {
    const state = payload.details?.state ? ` · ${payload.details.state}` : "";
    return `${payload.strategy} · ${payload.action}${state} · ${(payload.reasons || []).join(" · ")}`;
  }
  if (event.event === "trade_opened") return `${payload.plan?.side || ""} · entry ${price(payload.position?.entry)} · stop ${price(payload.position?.stop)} · target ${price(payload.position?.target)}`;
  if (event.event === "trade_closed") return `${payload.reason} · net ${money(payload.netPnl)} · MAE ${money(payload.maeUsd)} · MFE ${money(payload.mfeUsd)}`;
  if (event.event === "risk_reject") return payload.reason || "risk reject";
  if (event.event === "symbol_activated") return "Монета выбрана сканером и переведена в активное наблюдение";
  return event.event;
}

function renderEvents() {
  $("replayEvents").innerHTML = (bundle?.events || []).map((event, index) => `<button class="replay-event" data-event-index="${index}">
    <time>${new Date(event.ts * 1000).toLocaleTimeString()}</time><strong>${event.event}</strong><span>${eventText(event)}</span>
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
    eventText(event)
  ];
  if (payload.plan) lines.push(`notional ${money(payload.plan.notional)} · expected net ${money(payload.plan.expected_net_profit)} · costs ${money(payload.plan.estimated_costs)}`);
  $("replayDecision").textContent = lines.join("\n");
}

function renderOverlayForFrame(frame) {
  clearOverlays();
  applyPosition(frame?.position);
  if (!selectedEvent) return;
  const payload = selectedEvent.payload || {};
  applyVisuals(payload.visuals || payload.decision?.visuals);
  if (payload.plan) {
    addPriceLine(payload.plan.market_entry, "entry", "#007aff", 0);
    addPriceLine(payload.plan.stop, "stop", "#ff3b30", 2);
    addPriceLine(payload.plan.target, "target", "#34c759", 2);
  }
  if (payload.watched_level != null) addPriceLine(payload.watched_level, "watched", "#8e8e93", 2);
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
  $("replayPosition").textContent = frame.position ? `${frame.position.side.toUpperCase()} ${money(frame.position.unrealized_pnl)}` : "Нет";
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
  candleSeries.setMarkers([]);
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
