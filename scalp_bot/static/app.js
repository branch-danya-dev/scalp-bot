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
let domDisplayDepth = 12;
let strategyEventFilter = "all";
let eventTypeFilter = "all";
let selectedReviewSession = "current";
let lastLiveClosedTrades = [];
let selectedTradeReviewId = null;
const tradeReviewCache = new Map();

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
const bps = value => value == null ? "—" : `${(Number(value) * 10_000).toFixed(1)} bps`;
const clock = value => value == null ? "—" : new Date(Number(value) * 1000).toLocaleTimeString();
function duration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = String(Math.floor(total / 3600)).padStart(2, "0");
  const m = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const s = String(total % 60).padStart(2, "0");
  return `${h}:${m}:${s}`;
}

const STRATEGY_LABELS = {
  trend_structure: "Трендовый откат",
  weak_level_rejection: "Отбой от уровня",
  orderbook_density: "Ликвидность стакана",
  level_breakout: "Пробой уровня",
};
const STATE_LABELS = {
  search: "поиск", found: "найден", persisting: "удерживается",
  approach: "подход", pressure: "давление", test: "тест",
  defended: "защищена", exhausted: "исчерпана", reaction: "реакция",
  reject: "отбой", break: "пробой", impulse: "импульс",
  pullback: "откат", reclaim: "возврат", continuation: "продолжение",
  stale_book: "стакан устарел", stale_candle: "1m история устарела", watch: "наблюдение", unknown: "неизвестно",
};
const ACTION_LABELS = {wait:"ЖДЁМ", long:"ЛОНГ", short:"ШОРТ"};
const TREND_LABELS = {up:"ВВЕРХ", down:"ВНИЗ", flat:"БОКОВИК"};
const HTF_BIAS_LABELS = {bullish:"бычий", bearish:"медвежий", neutral:"нейтральный"};
const LOCAL_REGIME_LABELS = {
  bullish_impulse:"бычий импульс",
  bearish_impulse:"медвежий импульс",
  bullish_trend:"локальный рост",
  bearish_trend:"локальное снижение",
  pullback:"откат",
  transition:"переход",
  range:"диапазон",
  unclear:"неясно",
};
const SIDE_LABELS = {long:"ЛОНГ", short:"ШОРТ", buy:"ПОКУПКА", sell:"ПРОДАЖА", bid:"BID", ask:"ASK"};
const EVENT_LABELS = {
  decision:"Решение", entry_pending:"Лимитный вход ожидает", entry_cancelled:"Лимитный вход отменён",
  trade_opened:"Вход", partial_take:"Частичная фиксация",
  trade_closed:"Выход", risk_reject:"Отклонено риском", economic_shadow:"Экономика (shadow)",
  setup_blocked:"Сетап заблокирован", setup_consumed:"Сетап использован",
  setup_rearmed:"Сетап переактивирован", symbol_activated:"Монета активирована",
  symbol_deactivated:"Монета исключена", run_summary:"Итог прогона",
  bot_started:"Прогон запущен", bot_stopped:"Прогон остановлен",
  startup_scan_error:"Ошибка стартового сканера", scanner_error:"Ошибка сканера",
  symbol_bootstrap_error:"Ошибка загрузки рынка", context_error:"Ошибка контекста",
  strategy_error:"Ошибка стратегии", candle_resync:"1m история восстановлена",
  market_context_changed:"Рыночный режим изменился", entry_freshness_changed:"Свежесть входа изменилась",
};
const TRACE_PHRASES = {
  "confirmed trend structure and a valid trendline":"подтверждённая структура тренда и валидная трендовая линия",
  "actual test of the trend support/resistance":"фактический тест трендовой поддержки/сопротивления",
  "reclaim of the trendline and aligned local tape":"возврат за трендовую линию и подтверждение локальным потоком",
  "follow-through in the trend direction":"продолжение движения по тренду",
  "risk and execution approval":"одобрение риска и исполнения",
  "fresh/young horizontal level":"свежий горизонтальный уровень",
  "directional approach to the level":"направленный подход к уровню",
  "real break beyond the zone and reclaim":"реальный выход за зону с возвратом",
  "fresh local tape reversal at the level":"свежий локальный разворот потока у уровня",
  "significant observable order-book wall":"значимая наблюдаемая стенка в стакане",
  "wall persistence and stability":"устойчивость и стабильность стенки",
  "price approach to the wall":"подход цены к стенке",
  "actual trade touch of the wall":"фактическое касание стенки сделками",
  "price reaction and fresh local tape reversal":"реакция цены и свежий разворот локального потока",
  "mature worked horizontal zone":"зрелая проторгованная горизонтальная зона",
  "directional pressure into the zone":"направленное давление в зону",
  "actual break of the zone":"фактический пробой зоны",
  "executed-flow acceptance beyond the broken edge":"закрепление исполненного потока за пробитой границей",
  "trend direction aligned":"направление совпадает с трендом",
  "flow confirmed":"поток подтверждён",
  "directional pullback observed":"направленный откат подтверждён",
  "absorption observed":"наблюдается поглощение",
  "density still fresh":"плотность остаётся свежей",
  "weak/fresh level identified":"обнаружен свежий/слабый уровень",
  "market context":"рыночный контекст",
  "level zone":"зона уровня",
  "watched level":"наблюдаемый уровень",
  "support":"поддержка",
  "resistance":"сопротивление",
};

function strategyLabel(value) { return STRATEGY_LABELS[value] || String(value || "—").replaceAll("_", " "); }
function stateLabel(value) { return STATE_LABELS[String(value || "unknown")] || String(value || "—").replaceAll("_", " "); }
function trendLabel(value) { return TREND_LABELS[String(value || "flat")] || String(value || "—").toUpperCase(); }
function htfBiasLabel(value) { return HTF_BIAS_LABELS[String(value || "neutral")] || String(value || "—").replaceAll("_", " "); }
function localRegimeLabel(value) { return LOCAL_REGIME_LABELS[String(value || "unclear")] || String(value || "—").replaceAll("_", " "); }
function entryFreshnessLabel(value) {
  return ({
    fresh:"свежий",
    acceptable:"допустимый",
    late:"поздний",
    exhausted:"импульс исчерпан",
    unknown:"нет оценки",
  })[String(value || "unknown")] || String(value || "—").replaceAll("_", " ");
}
function flowAlignmentLabel(value) {
  return ({
    strongly_aligned:"полностью согласован",
    aligned:"согласован",
    short_term_reversal:"5с разворот против старшего потока",
    mixed:"смешанный",
    opposed:"против сделки",
    insufficient_data:"мало данных",
  })[String(value || "insufficient_data")] || String(value || "—").replaceAll("_", " ");
}
function liquidityStateLabel(value) {
  return ({
    none:"нет wall",
    tracking:"отслеживается",
    absorbing:"поглощение",
    replenishing:"пополнение",
    defended:"защищена",
    consumed:"съедается",
    removed:"снята",
    lost_significance:"потеряла значимость",
    unknown:"неизвестно",
  })[String(value || "unknown")] || String(value || "—").replaceAll("_", " ");
}
function liquidityAlignmentLabel(value) {
  return ({
    supportive:"поддерживает сделку",
    opposed:"против сделки",
    neutral:"нейтрально",
    unknown:"неизвестно",
  })[String(value || "unknown")] || String(value || "—").replaceAll("_", " ");
}
function sideLabel(value) { return SIDE_LABELS[String(value || "").toLowerCase()] || String(value || "—").toUpperCase(); }
function actionLabel(value) { return ACTION_LABELS[String(value || "wait")] || String(value || "—").toUpperCase(); }
function eventLabel(value) { return EVENT_LABELS[value] || String(value || "").replaceAll("_", " "); }
function translatePhrase(value) {
  const text = String(value || "");
  if (TRACE_PHRASES[text]) return TRACE_PHRASES[text];
  const progressed = text.match(/^strategy progressed to (.+)$/);
  if (progressed) return `стратегия перешла в состояние «${stateLabel(progressed[1])}»`;
  return text;
}
function marketObjectLabel(value) {
  const text = String(value || "");
  if (TRACE_PHRASES[text]) return TRACE_PHRASES[text];
  const density = text.match(/^(bid|ask) density$/i);
  if (density) return `${density[1].toUpperCase()} · плотность`;
  const tfLevel = text.match(/^(\S+)\s+(support|resistance)$/i);
  if (tfLevel) return `${tfLevel[1]} · ${TRACE_PHRASES[tfLevel[2].toLowerCase()]}`;
  return text.replaceAll("_", " ");
}
function reasonText(value) {
  const text = String(value || "");
  if (!text) return "—";
  if (text === "portfolio risk budget exhausted") return "Исчерпан лимит риска портфеля";
  if (text === "portfolio exposure budget exhausted") return "Исчерпан лимит экспозиции портфеля";
  if (text === "insufficient visible entry depth after risk sizing") return "Недостаточная видимая глубина после расчёта риска";
  if (text.startsWith("insufficient visible entry depth:")) return text.replace("insufficient visible entry depth:", "Недостаточная видимая глубина входа:");
  if (text.startsWith("setup expired after depth: entry drift")) return text.replace("setup expired after depth: entry drift", "Сетап устарел после проверки глубины: дрейф входа");
  if (text.startsWith("setup expired: entry drift")) return text.replace("setup expired: entry drift", "Сетап устарел: дрейф входа");
  if (text.startsWith("net at target")) return text.replace("net at target", "Net на цели").replace("after estimated trading costs", "после расчётных торговых издержек").replace("required", "требуется");
  if (text.startsWith("movement_gate: first_take_move")) return text.replace("movement_gate: first_take_move", "Минимальное движение до первого тейка");
  if (text.startsWith("economic_gate: winner_cost_share")) return text.replace("economic_gate: winner_cost_share", "Экономика: доля издержек winner");
  if (text.startsWith("economic_gate: stop_cost_share")) return text.replace("economic_gate: stop_cost_share", "Экономика: доля издержек stop");
  if (text === "passive_entry_timeout") return "PostOnly вход не исполнился до таймаута";
  if (text.startsWith("passive_fill_blocked:")) return text.replace("passive_fill_blocked:", "PostOnly fill отменён:");
  if (text === "passive_fill_exposure_budget") return "PostOnly fill отменён: исчерпан лимит экспозиции";
  if (text === "passive_fill_risk_budget") return "PostOnly fill отменён: исчерпан лимит риска";
  if (text.includes("economic_gate: insufficient_net_reward_risk")) return text.replace("economic_gate: insufficient_net_reward_risk:", "Экономика: недостаточный net R:R:");
  if (text === "setup consumed") return "сетап использован";
  if (text === "rearmed") return "переактивирован";
  if (text === "deactivated") return "исключена из наблюдения";
  if (text === "stopped") return "остановлено";
  if (text === "closed") return "закрыто";
  if (text === "target") return "цель";
  if (text === "runner_target") return "цель раннера";
  if (text === "stop") return "стоп";
  if (text === "no_follow_through") return "нет продолжения движения";
  if (text === "partial_take") return "частичная фиксация";
  if (text === "duration_elapsed") return "время прогона истекло";
  if (text === "bot_stop") return "остановлено пользователем";
  if (text === "shutdown") return "завершение приложения";
  return translatePhrase(text);
}

function timeframeUsesSeconds(timeframe) { return timeframe === "5s" || timeframe === "15s"; }
function applyChartTimeframeScale() {
  if (!chart) return;
  const withSeconds = timeframeUsesSeconds(selectedChartTimeframe);
  chart.applyOptions({
    timeScale: {
      timeVisible:true, secondsVisible:withSeconds, borderVisible:false,
      tickMarkFormatter: time => new Date(Number(time) * 1000).toLocaleTimeString([],
        withSeconds
          ? {hour:"2-digit", minute:"2-digit", second:"2-digit"}
          : {hour:"2-digit", minute:"2-digit"})
    }
  });
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
      secondsVisible:timeframeUsesSeconds(selectedChartTimeframe),
      borderVisible:false,
      tickMarkFormatter: time => new Date(Number(time) * 1000).toLocaleTimeString([],
        timeframeUsesSeconds(selectedChartTimeframe)
          ? {hour:"2-digit", minute:"2-digit", second:"2-digit"}
          : {hour:"2-digit", minute:"2-digit"})
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
    const title = `${marketObjectLabel(level.kind)} · ${tf}`;
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
    const label = `${strategyLabel(decision.strategy)} · ${stateLabel(trace.state || decision.details?.state || "")}`;
    if (object.low != null && object.high != null) {
      addPriceLine(object.low, label + " · низ", "#007aff", 0, 2, true);
      addPriceLine(object.high, label + " · верх", "#007aff", 0, 2, true);
    } else if (object.price != null) {
      addPriceLine(object.price, label, "#007aff", 0, 2, true);
    } else if (decision.watched_level != null) {
      addPriceLine(decision.watched_level, label, "#007aff", 0, 2, true);
    }
    const target = decision.details?.liquidityTarget?.price;
    if (target != null) addPriceLine(target, "цель по ликвидности", "#34c759", 2, 1, true);
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
    addPriceLine(position.entry, "ВХОД", "#007aff", 0, 2, true);
    addPriceLine(position.stop, "СТОП", "#ff3b30", 0, 2, true);
    addPriceLine(position.target, "ЦЕЛЬ", "#34c759", 0, 2, true);
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

function renderSymbolMeta(market) {
  if (!market) return;
  const book = market.orderbook || {};
  const mid = book.bestBid && book.bestAsk ? (book.bestBid + book.bestAsk) / 2 : null;
  const gapBps = mid ? (market.lastPrice - mid) / mid * 10000 : null;
  const gapText = gapBps == null ? "" : ` · цена↔стакан ${gapBps >= 0 ? "+" : ""}${gapBps.toFixed(1)} bps`;
  const flow = market.tradeFlow || {};
  const flowText = flow.tradeCount5s
    ? ` · поток 5с ${(Number(flow.imbalance5s || 0) * 100).toFixed(0)}% · скорость x${Number(flow.acceleration || 0).toFixed(1)}`
    : "";
  const profile = market.activityProfile || {};
  const corr = profile.correlation_1h_btc == null
    ? "корр. 1ч: н/д"
    : `корр. BTC 1ч ${(Number(profile.correlation_1h_btc) * 100).toFixed(0)}%`;
  const trades24h = profile.trade_count_24h == null
    ? "сделки 24ч: н/д"
    : `сделки 24ч ${compact(profile.trade_count_24h)}`;
  const timeframeRows = market.chartSeries?.[selectedChartTimeframe] || [];
  const latestBar = timeframeRows.length ? timeframeRows[timeframeRows.length - 1] : null;
  const barState = latestBar?.confirmed === false ? "формируется" : latestBar ? "закрыта" : "нет данных";
  const context = market.marketContext || {};
  const htf = context.htfBias || {};
  const local = context.localRegime || {};
  const multiFlow = context.flowContext || {};
  const dominantFlow = multiFlow.dominantDirection
    ? trendLabel(multiFlow.dominantDirection)
    : "—";
  const liquidity = context.liquidityEvidence || {};
  const liquidityText = liquidity.state
    ? `${liquidityStateLabel(liquidity.state)}${liquidity.directionalBias && liquidity.directionalBias !== "flat" ? "→" + trendLabel(liquidity.directionalBias) : ""}`
    : "—";
  const execution = context.executionContext || {};
  const executionText = execution.ready === true
    ? "exec ok"
    : execution.ready === false
      ? "exec stale"
      : "exec —";
  const structure = context.structureContext || {};
  const nearest = [];
  if (structure.supportDistancePct != null) nearest.push(`S ${(Number(structure.supportDistancePct) * 10000).toFixed(0)}bps`);
  if (structure.resistanceDistancePct != null) nearest.push(`R ${(Number(structure.resistanceDistancePct) * 10000).toFixed(0)}bps`);
  const structureText = nearest.length ? nearest.join("/") : "S/R —";
  const contextText = ` · HTF ${htfBiasLabel(htf.bias)} · локально: ${localRegimeLabel(local.regime)} · flow: ${dominantFlow} · liq: ${liquidityText} · ${executionText} · ${structureText}`;
  $("symbolMeta").textContent = `${selectedChartTimeframe} · ${barState} · цена ${price(market.lastPrice)}${gapText} · 24ч ${pct(profile.change_24h)} · оборот ${compact(profile.turnover_24h)} · ${corr} · ${trades24h} · активность ${Number(profile.activity_score || 0).toFixed(0)}${contextText}${flowText}`;
}

function bindChartControls() {
  document.querySelectorAll("[data-timeframe]").forEach(button => {
    button.onclick = () => {
      selectedChartTimeframe = button.dataset.timeframe;
      document.querySelectorAll("[data-timeframe]").forEach(row =>
        row.classList.toggle("active", row.dataset.timeframe === selectedChartTimeframe)
      );
      applyChartTimeframeScale();
      renderMarketChart(lastMarketForChart, lastPositionForChart);
      renderSymbolMeta(lastMarketForChart);
      if (chart) chart.timeScale().fitContent();
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
    const position = row.position
      ? `<span class="pill">${sideLabel(row.position.side)}</span>`
      : `<span title="HTF ${htfBiasLabel(row.htfBias)} · legacy ${trendLabel(row.trend)}">${localRegimeLabel(row.localRegime)}</span>`;
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

function renderBook(book, densityContext=null) {
  if (!book) return;
  const max = Math.max(1, ...book.bids.map(x => x[2]), ...book.asks.map(x => x[2]));
  const wallPrice = Number(densityContext?.wallPrice);
  const isWall = item => Number.isFinite(wallPrice)
    && Math.abs(Number(item[0]) - wallPrice) / Math.max(Math.abs(wallPrice), 1e-9) <= 1e-9;
  const row = (item, kind) => `<div class="book-row ${kind} ${isWall(item) ? "bot-wall" : ""}" style="--depth:${Math.max(3, item[2] / max * 100)}%">
    <span>${price(item[0])}</span><span>${Number(item[1]).toFixed(3)}</span><span>${compact(item[2])}</span>
  </div>`;
  $("asks").innerHTML = [...book.asks].slice(0, domDisplayDepth).reverse().map(x => row(x, "ask")).join("");
  $("bids").innerHTML = book.bids.slice(0, domDisplayDepth).map(x => row(x, "bid")).join("");
  $("midPrice").textContent = book.bestBid && book.bestAsk ? price((book.bestBid + book.bestAsk) / 2) : "—";
  $("spread").textContent = `спред ${(book.spreadPct * 100).toFixed(4)}%`;
}

function domPct(value) {
  return value == null ? "—" : `${(Number(value) * 100).toFixed(1)}%`;
}

function renderDomInspector(context) {
  const root = $("domInspector");
  if (!root) return;
  if (!context) {
    root.innerHTML = '<div class="dom-empty">Стратегия плотности сейчас не отслеживает активную стенку.</div>';
    return;
  }
  const flow = context.recentLevelFlow || context.levelFlow || {};
  const ofi = context.bookFlow || {};
  const state = stateLabel(context.state || "watch");
  root.innerHTML = `
    <div class="dom-state-line">
      <strong>${String(context.wallSide || "").toUpperCase()} · СТЕНКА · ${price(context.wallPrice)}</strong>
      <span class="dom-state">${state}</span>
    </div>
    <div class="dom-metrics">
      <span><small>Стенка</small>${compact(context.notionalUsd)}</span>
      <span><small>Сила</small>${Number(context.strengthMultiple || 0).toFixed(1)}x</span>
      <span><small>Осталось</small>${domPct(context.remainingRatio)}</span>
      <span><small>Атака 5с</small>${compact(context.attackNotional5s)}</span>
      <span><small>Истощение/с</small>${domPct(context.depletionPerSecond)}</span>
      <span><small>Пополнение</small>${domPct(context.replenishmentRatio)}</span>
      <span><small>Локальный поток</small>${flow.imbalance == null ? "—" : (Number(flow.imbalance) * 100).toFixed(0) + "%"}</span>
      <span><small>OFI 5s</small>${compact(ofi.bestLevelOfiUsd5s)}</span>
    </div>
    <div class="dom-flags">
      <span class="${context.absorptionObserved ? "flag good" : "flag"}">поглощение: ${context.absorptionObserved ? "ДА" : "нет"}</span>
      <span class="${context.wallPresent === false ? "flag bad" : "flag"}">стенка: ${context.wallPresent === false ? "снята" : "на месте"}</span>
      <span class="${context.positionInvalidated ? "flag bad" : "flag"}">инвалидация: ${context.positionInvalidated ? "ДА" : "нет"}</span>
    </div>
  `;
}

function bindDomControls() {
  document.querySelectorAll("[data-dom-depth]").forEach(button => {
    button.onclick = () => {
      domDisplayDepth = Number(button.dataset.domDepth);
      document.querySelectorAll("[data-dom-depth]").forEach(row =>
        row.classList.toggle("active", Number(row.dataset.domDepth) === domDisplayDepth)
      );
      if (lastMarketForChart) {
        renderBook(lastMarketForChart.orderbook, lastMarketForChart.densityContext);
      }
    };
  });
}

function renderStrategies(rows) {
  $("strategyList").innerHTML = rows.map(row => {
    const stats = row.stats || {};
    const netClass = Number(stats.netPnl || 0) >= 0 ? "positive" : "negative";
    const stateCounts = stats.stateCounts || {};
    const funnelOrder = row.key === "trend_structure"
      ? ["search", "pullback", "test", "reclaim", "continuation"]
      : row.key === "weak_level_rejection"
        ? ["search", "found", "approach", "test", "reject", "reaction"]
        : row.key === "orderbook_density"
          ? ["search", "found", "persisting", "approach", "test", "reaction", "exhausted"]
          : ["search", "found", "approach", "pressure", "break", "impulse"];
    const funnel = funnelOrder
      .filter(state => Number(stateCounts[state] || 0) > 0)
      .map(state => `<span><small>${stateLabel(state)}</small>${Number(stateCounts[state] || 0)}</span>`)
      .join("");
    return `<div class="strategy-card">
      <div class="strategy-row">
        <span><strong>${row.label}</strong><small>${row.key}</small></span>
        <button class="switch ${row.enabled ? "on" : ""}" data-strategy="${row.key}" data-enabled="${row.enabled}"></button>
      </div>
      <div class="strategy-stats">
        <span><small>Уникальные сетапы</small>${stats.uniqueTradeableSetups ?? stats.tradeableSignals ?? 0}</span>
        <span><small>Сделки</small>${stats.tradesClosed || 0}</span>
        <span><small>П/У</small>${stats.wins || 0}/${stats.losses || 0}</span>
        <span class="${netClass}"><small>Net PnL</small>${money(stats.netPnl || 0)}</span>
        <span><small>Уникальные отказы</small>${stats.uniqueRiskRejectedSetups ?? stats.riskRejects ?? 0}</span>
        <span title="Все изменения состояния/цены одного и того же сетапа"><small>Updates</small>${stats.decisionUpdates ?? stats.decisions ?? 0}</span>
      </div>
      ${funnel ? `<div class="strategy-funnel">${funnel}</div>` : ""}
    </div>`;
  }).join("");
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

function traceObjectText(object={}) {
  if (object.low != null && object.high != null) {
    return `${marketObjectLabel(object.label || object.type)}: ${price(object.low)}–${price(object.high)}`;
  }
  if (object.price != null) {
    return `${marketObjectLabel(object.label || object.type)}: ${price(object.price)}`;
  }
  return marketObjectLabel(object.label || object.type || "market context");
}

function renderDecisions(decisions) {
  const rows = Object.values(decisions || {});
  $("decisionStrip").innerHTML = rows.map(decision => {
    const trace = decision.trace || {};
    const state = trace.state || decision.details?.state || "unknown";
    const observed = trace.observedAtMs
      ? new Date(trace.observedAtMs).toLocaleTimeString()
      : "—";
    const confirmed = (trace.confirmed || []).map(row => `<span class="trace-tag confirmed">${translatePhrase(row)}</span>`).join("");
    const waiting = (trace.waitingFor || []).map(row => `<li>${translatePhrase(row)}</li>`).join("");
    const flowAlignment = decision.details?.flowAlignment || trace.evidence?.flowAlignment;
    const flowText = flowAlignment
      ? ` · flow <b>${flowAlignmentLabel(flowAlignment.classification)}</b>${flowAlignment.score == null ? "" : " (" + Number(flowAlignment.score).toFixed(2) + ")"}`
      : "";
    const liquidityAlignment = decision.details?.liquidityAlignment || trace.evidence?.liquidityAlignment;
    const liquidityEvidence = decision.details?.liquidityEvidence || trace.evidence?.liquidityEvidence;
    const liquidityText = liquidityEvidence
      ? ` · liq <b>${liquidityStateLabel(liquidityEvidence.state)}</b>${liquidityAlignment ? " · " + liquidityAlignmentLabel(liquidityAlignment.classification) : ""}`
      : "";
    const actionText = decision.details?.evidenceOnly
      ? "EVIDENCE"
      : actionLabel(decision.action);
    const decisionContext = decision.details?.decisionContext || trace.evidence?.decisionContext || {};
    const contextSnapshotText = decisionContext.localRegime
      ? ` · ctx <b>${localRegimeLabel(decisionContext.localRegime)}</b>${decisionContext.executionReady === false ? " · exec stale" : ""}`
      : "";
    const playbookContext = decision.details?.playbookContext || trace.evidence?.playbookContext || {};
    const playbookText = playbookContext.primaryDirection
      ? ` · playbook <b>${trendLabel(playbookContext.primaryDirection)}</b>${playbookContext.source ? " (" + String(playbookContext.source).replaceAll("_", " ") + ")" : ""}`
      : "";
    const entryAssessment = decision.details?.entryContextAssessment || trace.evidence?.entryContextAssessment || {};
    const blockerText = entryAssessment.allowed === false && Array.isArray(entryAssessment.blockers)
      ? ` · blocked: ${entryAssessment.blockers.join(", ")}`
      : "";
    return `<article class="decision-card">
      <div class="decision-card-head">
        <div>
          <strong>${strategyLabel(decision.strategy)}</strong>
          <span>${actionText} · ${stateLabel(state)}</span>
        </div>
        <time>${observed}</time>
      </div>
      <div class="decision-object">${traceObjectText(trace.object)}</div>
      <div class="decision-context">Тренд legacy: <b>${trendLabel(trace.trend)}</b> · уверенность ${Number(trace.confidence || 0).toFixed(2)}${playbookText}${flowText}${liquidityText}${contextSnapshotText}${blockerText}</div>
      <div class="trace-tags">${confirmed || '<span class="trace-tag">нет подтверждений</span>'}</div>
      ${waiting ? `<div class="decision-wait"><small>Чего ждём</small><ul>${waiting}</ul></div>` : ""}
    </article>`;
  }).join("");
}

function eventText(event) {
  const payload = event.payload || {};
  if (event.event === "entry_pending") return `PostOnly @ ${price(payload.pending?.limitPrice ?? payload.plan?.market_entry)} · ${money(payload.plan?.notional)}`;
  if (event.event === "entry_cancelled") return `${reasonText(payload.reason)} · ${price(payload.limitPrice)}`;
  if (event.event === "trade_opened") { const details = payload.plan?.strategy_details || {}; const fa = details.flowAlignment; const la = details.liquidityAlignment; return `${sideLabel(payload.plan?.side)} · ${money(payload.plan?.notional)} · net на цели ${money(payload.plan?.net_at_target ?? payload.plan?.expected_net_profit)} · качество ${Number(payload.opportunityQuality ?? 0).toFixed(2)}${fa ? " · flow " + flowAlignmentLabel(fa.classification) : ""}${la ? " · liq " + liquidityAlignmentLabel(la.classification) : ""}`; }
  if (event.event === "partial_take") return `частичная фиксация ${money(payload.netPnl)} · осталось ${money(payload.remainingNotional)} · стоп→${price(payload.newStop)}`;
  if (event.event === "trade_closed") return `${reasonText(payload.reason)} · ${payload.exitMoveBps == null ? "—" : Number(payload.exitMoveBps).toFixed(1) + " bps"} · комиссия ${money(payload.fees)} · net ${money(payload.netPnl)}`;
  if (event.event === "risk_reject") {
    const d = payload.diagnostics || {};
    const economics = d.netAtTargetUsd != null
      ? ` · net ${money(d.netAtTargetUsd)} / риск ${money(d.allInNetLossUsd)} / R:R ${Number(d.netRewardRisk || 0).toFixed(2)}`
      : "";
    return `${reasonText(payload.reason || "отклонено")}${economics}`;
  }
  if (event.event === "economic_shadow") return `наблюдение: ${(payload.shadowRejectReasons || []).map(reasonText).join(", ")}`;
  if (event.event === "setup_blocked") return `${strategyLabel(payload.strategy)}: ${reasonText(payload.reason)}`;
  if (event.event === "setup_consumed") return `${strategyLabel(payload.strategy)}: сетап использован`;
  if (event.event === "setup_rearmed") return `${strategyLabel(payload.strategy)}: переактивирован`;
  if (event.event === "decision") {
    const details = payload.details || {};
    const metrics = [];
    if (details.distancePct != null) metrics.push(`до опоры ${(Number(details.distancePct) * 10000).toFixed(1)} bps`);
    if (details.testDistancePct != null) metrics.push(`test ${(Number(details.testDistancePct) * 10000).toFixed(1)} bps`);
    if (details.deepPenetrationPct != null) metrics.push(`penetration ${(Number(details.deepPenetrationPct) * 10000).toFixed(1)} bps`);
    return `${strategyLabel(payload.strategy)} · ${stateLabel(details.state)}: ${(payload.reasons || []).map(reasonText).join(" · ")}${metrics.length ? " · " + metrics.join(" · ") : ""}`;
  }
  if (event.event === "symbol_activated") return "монета стала активной";
  if (event.event === "symbol_deactivated") return reasonText(payload.reason || "deactivated");
  if (event.event === "run_summary") return `${reasonText(payload.reason)} · прошло ${duration(payload.elapsedSeconds)} · PnL ${money(payload.realizedPnl)} · сделок ${payload.closedTrades}`;
  if (event.event === "bot_stopped") return reasonText(payload.reason || "stopped");
  if (event.event === "startup_scan_error") return payload.error || "Ошибка стартового сканера";
  if (event.event === "scanner_error") return payload.error || "Ошибка сканера";
  if (event.event === "symbol_bootstrap_error") return payload.error || "Ошибка загрузки рынка";
  if (event.event === "context_error") return payload.error || "Ошибка обновления контекста";
  if (event.event === "strategy_error") return payload.error || "Ошибка стратегии";
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

function eventStrategy(event) {
  const payload = event.payload || {};
  return payload.strategy
    || payload.plan?.strategy
    || payload.decision?.strategy
    || payload.trace?.strategy
    || null;
}

function eventGroup(event) {
  if (event.event.endsWith("_error")) return "error";
  if (event.event === "trade_opened" || event.event === "entry_pending") return "entry";
  if (event.event === "entry_cancelled") return "reject";
  if (event.event === "trade_closed" || event.event === "partial_take") return "exit";
  if (event.event === "risk_reject" || event.event === "setup_blocked" || event.event === "economic_shadow") return "reject";
  if (event.event === "decision") return "decision";
  return "system";
}

function renderEvents(rows) {
  const filtered = compactEvents(rows).filter(event => {
    const strategy = eventStrategy(event);
    const strategyOk = strategyEventFilter === "all" || strategy === strategyEventFilter;
    const typeOk = eventTypeFilter === "all" || eventGroup(event) === eventTypeFilter;
    return strategyOk && typeOk;
  });
  $("events").innerHTML = filtered.slice(0, 80).map(event => `<div class="event">
    <time>${new Date(event.ts * 1000).toLocaleTimeString()}</time>
    <span class="type">${eventLabel(event.event)}</span>
    <span class="text">${event.symbol || ""} ${eventText(event)}</span>
  </div>`).join("");
}

async function loadReviewSession(sessionName) {
  const nextSession = sessionName || "current";
  if (nextSession !== selectedReviewSession) {
    closeTradeReview();
    tradeReviewCache.clear();
  }
  selectedReviewSession = nextSession;
  tradeReviewSummaries = [];
  lastClosedTradeCount = -1;
  try {
    const query = selectedReviewSession === "current"
      ? ""
      : `?session=${encodeURIComponent(selectedReviewSession)}`;
    const payload = await api(`/api/reviews/trades${query}`);
    tradeReviewSummaries = payload.reviews || [];
    if (selectedReviewSession === "current") {
      renderTrades(lastLiveClosedTrades);
    } else {
      renderTrades(tradeReviewSummaries);
    }
  } catch (error) {
    $("closedTrades").innerHTML = `<div class="review-error">${String(error.message || error)}</div>`;
  }
}

async function bindReviewSessions() {
  const select = $("reviewSessionSelect");
  if (!select) return;
  try {
    const payload = await api("/api/replay/sessions");
    const sessions = payload.sessions || [];
    select.innerHTML = '<option value="current">Текущая сессия</option>'
      + sessions.map(row => `<option value="${row.name}">${row.name}</option>`).join("");
  } catch (_) {
    select.innerHTML = '<option value="current">Текущая сессия</option>';
  }
  select.onchange = () => {
    loadReviewSession(select.value);
    const root = $("opportunityReview");
    if (root) root.innerHTML = '<div class="empty-row">Нажми «Пересчитать» для выбранной сессии.</div>';
  };
}

function bindEventFilters() {
  document.querySelectorAll("[data-event-strategy]").forEach(button => {
    button.onclick = () => {
      strategyEventFilter = button.dataset.eventStrategy;
      document.querySelectorAll("[data-event-strategy]").forEach(row =>
        row.classList.toggle("active", row.dataset.eventStrategy === strategyEventFilter)
      );
      refresh();
    };
  });
  document.querySelectorAll("[data-event-type]").forEach(button => {
    button.onclick = () => {
      eventTypeFilter = button.dataset.eventType;
      document.querySelectorAll("[data-event-type]").forEach(row =>
        row.classList.toggle("active", row.dataset.eventType === eventTypeFilter)
      );
      refresh();
    };
  });
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
  const phase = position.partial_taken ? "РАННЕР" : "ПОЛНАЯ ПОЗИЦИЯ";
  box.innerHTML = `<strong>${sideLabel(position.side)} ${position.symbol} · ${phase}</strong>
    <span>вход ${price(position.entry)} → сейчас ${price(position.last_price)}</span>
    <span>движение ${bps(position.current_move_pct)} · ${pct(position.current_move_pct)}</span>
    <span>первый тейк план ${bps(position.planned_first_take_move_pct)}</span>
    <span>стоп ${price(position.stop)} · цель ${price(position.target)}</span>
    <span class="${pnlClass}">открытый PnL ${money(position.unrealized_pnl)}</span>
    <span>комиссия начислена ${money(position.fees_committed_usd)} · если закрыть сейчас ≈ ${money(position.estimated_total_fees_if_close_now_usd)}</span>
    <span>MFE ${bps(position.max_favorable_move_pct)} @ ${clock(position.mfe_at)}</span>
    <span>MAE ${bps(position.max_adverse_move_pct)} @ ${clock(position.mae_at)}</span>
    <span>зафиксировано ${money(position.realized_net_usd)} · остаток ${money(position.notional)}</span>`;
}

function reviewClassLabel(value) {
  return ({
    missed_target_first:"Цель была раньше стопа",
    correct_reject_candidate:"Стоп был раньше цели",
    ambiguous:"Цель и стоп в одном кадре",
    unresolved:"Не разрешилось",
    early_exit_review:"После выхода цена дошла до цели",
    exit_supported_by_followup:"После выхода цель не достигнута",
  })[value] || value || "—";
}

function marketMoveVisibilityLabel(value) {
  return ({
    undetected:"Не замечено стратегиями",
    observed_not_tradeable:"Стратегия наблюдала, но не дала вход",
    detected_not_executed:"Сигнал был, но сделка не открылась",
    traded:"Движение было проторговано",
  })[value] || value || "—";
}

function marketMoveStrategyText(row) {
  const snapshots = row.strategySnapshot || [];
  if (!snapshots.length) return "нет решений стратегий перед импульсом";
  return snapshots.map(item => {
    const state = stateLabel(item.state);
    const action = item.action && item.action !== "wait"
      ? ` · ${String(item.action).toUpperCase()}`
      : "";
    return `${strategyLabel(item.strategy)}: ${state}${action}`;
  }).join(" · ");
}

function hindsightBotLabel(value) {
  return ({
    missed:"Бот пропустил",
    wrong_direction:"Бот торговал против движения",
    traded:"Позиция открыта в рабочем окне",
    late_entry:"Поздний вход",
    early_exit:"Ранний выход",
    late_entry_early_exit:"Поздний вход и ранний выход",
  })[value] || value || "—";
}

function hindsightStrategyFitText(row) {
  const fit = row.strategyFit || {};
  const strategies = fit.strategies || [];
  const useful = strategies
    .filter(item => item.fit !== "unaware")
    .map(item => {
      const statesSource = (item.alignedStatesSeen || []).length
        ? item.alignedStatesSeen
        : (item.opposedStatesSeen || []);
      const states = statesSource
        .map(stateLabel)
        .join("→");
      return `${strategyLabel(item.strategy)}: ${states || item.fit}`;
    });
  if (useful.length) return useful.join(" · ");
  return "ни одна текущая стратегия не описала раннюю фазу движения";
}

function renderOpportunityReview(report) {
  const root = $("opportunityReview");
  if (!root) return;

  const summary = report.summary || {};
  const hindsight = report.hindsight || {};
  const hindsightSummary = hindsight.summary || {};
  const hindsightRows = (hindsight.opportunities || [])
    .slice()
    .sort((left, right) => Number(right.estimatedNetMovePct || 0) - Number(left.estimatedNetMovePct || 0))
    .slice(0, 40);

  const candidateRows = (report.candidates || [])
    .filter(row => ["missed_target_first", "correct_reject_candidate", "ambiguous"].includes(row.classification))
    .slice(0, 20);
  const exitRows = (report.earlyExits || [])
    .filter(row => row.classification === "early_exit_review")
    .slice(0, 20);

  root.innerHTML = `
    <div class="opportunity-summary">
      <span><small>Возможности рынка</small>${hindsightSummary.opportunities || 0}</span>
      <span class="warn"><small>Пропущено ботом</small>${hindsightSummary.botMissed || 0}</span>
      <span class="good"><small>Покрыто стратегиями</small>${hindsightSummary.mappedToExistingStrategy || 0}</span>
      <span class="warn"><small>Нет подходящей стратегии</small>${hindsightSummary.unmappedToExistingStrategy || 0}</span>
      <span><small>Поздние входы</small>${hindsightSummary.botLateEntry || 0}</span>
      <span><small>Ранние выходы</small>${hindsightSummary.botEarlyExit || 0}</span>
    </div>

    <div class="opportunity-oracle">
      <div class="opportunity-oracle-head">
        <div>
          <h3>Hindsight-возможности по фактическому графику</h3>
          <small>Сначала находятся прибыльные движения рынка с учётом оценочных издержек. Решения стратегий используются только после этого — для диагностики и обучения.</small>
        </div>
        <small>минимум net ${pct(hindsight.policy?.minimumNetMovePct)} · оценочные round-trip costs ${bps(hindsight.policy?.estimatedRoundTripCostPct)}</small>
      </div>
      <div class="opportunity-list">
        ${hindsightRows.map(row => {
          const bot = row.botComparison || {};
          const fit = row.strategyFit || {};
          const netBps = Number(row.estimatedNetMovePct || 0) * 10000;
          const grossBps = Number(row.grossMovePct || 0) * 10000;
          const side = String(row.side || "").toUpperCase();
          const cls = bot.classification === "missed" || bot.classification === "wrong_direction"
            ? "market_move_undetected"
            : "market_move_detected";
          const playbook = fit.closestPlaybook
            ? strategyLabel(fit.closestPlaybook)
            : "нет соответствия";
          return `<div class="opportunity-row hindsight-opportunity ${cls}">
            <div>
              <strong>${row.symbol} · ${side}</strong>
              <span>${clock(row.oracleEntryTs)} → ${clock(row.oracleExitTs)}</span>
              <small>oracle ${price(row.oracleEntryPrice)} → ${price(row.oracleExitPrice)}</small>
            </div>
            <div>
              <span>${hindsightBotLabel(bot.classification)}</span>
              <small>ближайший playbook: ${playbook}</small>
              <small>${hindsightStrategyFitText(row)}</small>
            </div>
            <div>
              <span>gross ${grossBps.toFixed(1)} bps · net≈${netBps.toFixed(1)} bps</span>
              <small>окно входа до ${clock(row.entryWindowEndTs)} · окно выхода с ${clock(row.exitWindowStartTs)}</small>
            </div>
          </div>`;
        }).join("") || '<div class="empty-row">В записанном интервале не найдено движений, проходящих cost-aware критерий прибыльной возможности.</div>'}
      </div>
    </div>

    <div class="opportunity-diagnostics-title">
      <h3>Диагностика уже принятых решений бота</h3>
      <small>Вторичный слой: отклонённые входы и выходы анализируются отдельно от поиска возможностей рынка.</small>
    </div>
    <div class="opportunity-columns">
      <div>
        <h3>Отклонённые входы</h3>
        <div class="opportunity-list">
          ${candidateRows.map(row => `<div class="opportunity-row ${row.classification}">
            <div><strong>${row.symbol}</strong><span>${strategyLabel(row.strategy)} · ${eventLabel(row.sourceEvent)}</span></div>
            <div><span>${reviewClassLabel(row.classification)}</span><small>${reasonText(row.reason)}</small></div>
            <div><span>MFE ${row.mfeR == null ? "—" : Number(row.mfeR).toFixed(2) + "R"}</span><small>MAE ${row.maeR == null ? "—" : Number(row.maeR).toFixed(2) + "R"}</small></div>
          </div>`).join("") || '<div class="empty-row">Нет симулируемых отклонённых входов.</div>'}
        </div>
      </div>
      <div>
        <h3>Выходы для проверки</h3>
        <div class="opportunity-list">
          ${exitRows.map(row => `<div class="opportunity-row early_exit_review">
            <div><strong>${row.symbol}</strong><span>${strategyLabel(row.strategy)} · ${reasonText(row.reason)}</span></div>
            <div><span>${reviewClassLabel(row.classification)}</span><small>MFE после выхода ${row.postExitMfeR == null ? "—" : Number(row.postExitMfeR).toFixed(2) + "R"}</small></div>
          </div>`).join("") || '<div class="empty-row">Нет ранних выходов, требующих проверки.</div>'}
        </div>
      </div>
    </div>
  `;
}

async function loadOpportunityReview() {
  const root = $("opportunityReview");
  if (root) root.innerHTML = '<div class="empty-row">Анализируем прошедший рынок…</div>';
  try {
    const query = new URLSearchParams({horizon:"120"});
    if (selectedReviewSession !== "current") {
      query.set("session", selectedReviewSession);
    }
    const report = await api(`/api/reviews/opportunities?${query.toString()}`);
    renderOpportunityReview(report);
  } catch (error) {
    if (root) root.innerHTML = `<div class="review-error">${String(error.message || error)}</div>`;
  }
}

function reviewSummaryFor(trade) {
  if (trade.reviewId) return trade;
  const matches = tradeReviewSummaries.filter(review =>
    review.symbol === trade.symbol
    && review.setupId === trade.setupId
    && review.strategy === trade.strategy
  );
  if (!matches.length) return null;
  const closedAt = Number(trade.closedAt || 0);
  if (!closedAt) return matches[matches.length - 1];
  return matches.slice().sort(
    (left, right) =>
      Math.abs(Number(left.closedAt || 0) - closedAt)
      - Math.abs(Number(right.closedAt || 0) - closedAt)
  )[0];
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
    const waiting = (trace.waitingFor || []).slice(0, 2).map(translatePhrase).join(" · ");
    const freshness = payload.details?.entryFreshness || trace.evidence?.entryFreshness;
    const freshnessText = freshness
      ? ` · вход ${entryFreshnessLabel(freshness.classification)}${freshness.moveSpentRatio == null ? "" : " · spent " + (Number(freshness.moveSpentRatio) * 100).toFixed(0) + "%"}${freshness.confirmationAgeSeconds == null ? "" : " · age " + Number(freshness.confirmationAgeSeconds).toFixed(1) + "с"}`
      : "";
    return `${strategyLabel(trace.strategy || payload.strategy)} · ${stateLabel(trace.state || payload.details?.state)}${objectText}${freshnessText}${waiting ? " · ждём: " + waiting : ""}`;
  }
  if (row.event === "trade_opened") {
    const position = payload.position || {};
    return `Позиция открыта @ ${price(position.entry)} · первый тейк ${bps(position.planned_first_take_move_pct)} · комиссия входа ${money(position.entry_fee_total_usd)}`;
  }
  if (row.event === "partial_take") return `Частичная фиксация · ${payload.moveBps == null ? "—" : Number(payload.moveBps).toFixed(1) + " bps"} · комиссия ${money(payload.fees)} · net ${money(payload.netPnl)}`;
  if (row.event === "trade_closed") return `${reasonText(payload.reason || "closed")} · движение ${payload.exitMoveBps == null ? "—" : Number(payload.exitMoveBps).toFixed(1) + " bps"} · комиссия ${money(payload.fees)} · net ${money(payload.netPnl)}`;
  if (row.event === "risk_reject") return `Отклонено риском · ${reasonText(payload.reason)}`;
  if (row.event === "setup_blocked") return `Сетап заблокирован · ${reasonText(payload.reason)}`;
  if (row.event === "entry_freshness_changed") {
    const freshness = payload.entryFreshness || {};
    return `Свежесть входа: ${entryFreshnessLabel(freshness.classification)}${freshness.moveSpentRatio == null ? "" : " · spent " + (Number(freshness.moveSpentRatio) * 100).toFixed(0) + "%"}${freshness.confirmationAgeSeconds == null ? "" : " · age " + Number(freshness.confirmationAgeSeconds).toFixed(1) + "с"}`;
  }
  return row.event.replaceAll("_", " ");
}

function destroyReviewChart(reviewId) {
  const chart = reviewCharts.get(reviewId);
  if (chart) chart.remove();
  reviewCharts.delete(reviewId);
}

function closeTradeReview() {
  for (const reviewId of Array.from(reviewCharts.keys())) {
    destroyReviewChart(reviewId);
  }
  selectedTradeReviewId = null;
  const inspector = $("tradeReviewInspector");
  const root = $("tradeReviewInspectorContent");
  if (inspector) inspector.classList.add("hidden");
  if (root) root.innerHTML = "";
  const rows = selectedReviewSession === "current"
    ? lastLiveClosedTrades
    : tradeReviewSummaries;
  if (rows?.length) renderTrades(rows);
}

function reviewBookRows(rows, side) {
  if (!rows?.length) return '<div class="empty-row">Нет уровней.</div>';
  return rows.slice(0, 8).map(row => {
    const notional = Number(row[2] ?? (Number(row[0]) * Number(row[1])));
    return `<div class="review-book-row ${side}">
      <span>${price(row[0])}</span>
      <span>${compact(row[1])}</span>
      <strong>${money(notional)}</strong>
    </div>`;
  }).join("");
}

function reviewSnapshotHtml(label, snapshot) {
  if (!snapshot) {
    return `<section class="review-snapshot"><h3>${label}</h3><div class="empty-row">Snapshot отсутствует.</div></section>`;
  }
  const book = snapshot.orderbook || {};
  const flow = snapshot.tradeFlow || {};
  const bookFlow = snapshot.bookFlow || {};
  const health = snapshot.bookHealth || {};
  const candleHealth = snapshot.candleHealth || {};
  const density = snapshot.densityContext || {};
  return `<section class="review-snapshot">
    <h3>${label}</h3>
    <div class="review-snapshot-metrics">
      <span><small>Цена</small>${price(snapshot.lastPrice)}</span>
      <span><small>Спред</small>${pct(book.spreadPct)}</span>
      <span><small>Стакан</small>${health.fresh === false ? "STALE" : "fresh"} · ${health.bidLevels ?? "—"}/${health.askLevels ?? "—"}</span>
      <span><small>1m история</small>${candleHealth.ageSeconds == null ? "—" : Number(candleHealth.ageSeconds).toFixed(1) + "s"}</span>
      <span><small>Flow 5s</small>${Number(flow.imbalance5s || 0).toFixed(3)} · ${flow.tradeCount5s ?? 0} trades</span>
      <span><small>CVD 5s</small>${money(flow.cvd5s || 0)}</span>
      <span><small>OFI 5s</small>${money(bookFlow.bestLevelOfiUsd5s || 0)}</span>
      <span><small>Top-5 depth</small>${money(bookFlow.top5DepthUsd || 0)}</span>
      <span><small>Density</small>${density.state ? stateLabel(density.state) : "—"}${density.wallPrice ? " @ " + price(density.wallPrice) : ""}</span>
    </div>
    <div class="review-book-grid">
      <div><div class="review-subtitle">ASK</div>${reviewBookRows(book.asks || [], "ask")}</div>
      <div><div class="review-subtitle">BID</div>${reviewBookRows(book.bids || [], "bid")}</div>
    </div>
  </section>`;
}

function renderTradeReviewDetail(reviewId, review) {
  if (selectedTradeReviewId !== reviewId) return;
  const inspector = $("tradeReviewInspector");
  const root = $("tradeReviewInspectorContent");
  if (!inspector || !root) return;

  for (const existingId of Array.from(reviewCharts.keys())) {
    if (existingId !== reviewId) destroyReviewChart(existingId);
  }
  destroyReviewChart(reviewId);

  inspector.classList.remove("hidden");
  const summary = review.summary || {};
  const details = review.strategyDetails || {};
  const economics = details.economics || review.plan?.strategy_details?.economics || {};
  const plan = review.plan || {};
  $("tradeReviewInspectorMeta").textContent =
    `${summary.symbol || "—"} · ${strategyLabel(summary.strategy)} · ${reasonText(summary.reason)}`;

  root.innerHTML = `
    <div class="review-summary-metrics">
      <span><small>Net</small><strong class="${Number(summary.netPnl || 0) >= 0 ? "positive" : "negative"}">${money(summary.netPnl)}</strong></span>
      <span><small>Gross</small><strong>${money(summary.grossPnl)}</strong></span>
      <span><small>Комиссии</small><strong>${money(summary.fees)}</strong></span>
      <span><small>Движение вход→выход</small><strong>${summary.exitMoveBps == null ? "—" : Number(summary.exitMoveBps).toFixed(1) + " bps"}</strong></span>
      <span><small>Первый тейк план</small><strong>${bps(summary.plannedFirstTakeMovePct)}</strong></span>
      <span><small>MFE</small><strong>${summary.maxFavorableMoveBps == null ? "—" : Number(summary.maxFavorableMoveBps).toFixed(1) + " bps"} @ ${clock(summary.mfeAt)}</strong></span>
      <span><small>MAE</small><strong>${summary.maxAdverseMoveBps == null ? "—" : Number(summary.maxAdverseMoveBps).toFixed(1) + " bps"} @ ${clock(summary.maeAt)}</strong></span>
      <span><small>Длительность</small><strong>${duration(summary.durationSeconds)}</strong></span>
      <span><small>Notional</small><strong>${money(summary.originalNotional)}</strong></span>
      <span><small>Winner cost share</small><strong>${economics.winnerCostShare == null ? "—" : (Number(economics.winnerCostShare) * 100).toFixed(1) + "%"}</strong></span>
      <span><small>Плановый net R:R</small><strong>${economics.netRewardRisk == null ? "—" : Number(economics.netRewardRisk).toFixed(2)}</strong></span>
    </div>
    <div class="review-grid">
      <div class="review-chart" data-review-chart="${reviewId}"></div>
      <div class="review-timeline">
        <div class="review-subtitle">Хронология решений</div>
        <div class="review-events">
          ${(review.timeline || []).map(row => `<div class="review-event">
            <time>${new Date(row.ts * 1000).toLocaleTimeString()}</time>
            <span class="review-event-type">${eventLabel(row.event)}</span>
            <span>${timelineText(row)}</span>
          </div>`).join("") || '<div class="empty-row">Хронология отсутствует.</div>'}
        </div>
      </div>
    </div>
    <div class="review-context">
      <div><b>Стратегия:</b> ${strategyLabel(summary.strategy)}</div>
      <div><b>Сетап:</b> ${summary.setupId || "—"}</div>
      <div><b>Вход:</b> ${price(summary.entry)} · <b>выход:</b> ${price(summary.exit)}</div>
      <div><b>Стоп:</b> ${price(summary.initialStop)} · <b>цель:</b> ${price(summary.target)}</div>
      <div><b>Источник цели:</b> ${marketObjectLabel(details.targetSource || "—")}</div>
      <div><b>Исполнение:</b> ${economics.executionProfile?.entry || plan.entry_mode || "—"} → ${economics.executionProfile?.target_exit || "—"}</div>
      <div><b>Lifecycle cost:</b> ${economics.lifecycleCostPct == null ? "—" : pct(economics.lifecycleCostPct)}</div>
      <div><b>Выход:</b> ${reasonText(summary.reason)}</div>
    </div>
    <div class="review-snapshots">
      ${reviewSnapshotHtml("На входе", review.openSnapshot)}
      ${reviewSnapshotHtml("На выходе", review.closeSnapshot)}
    </div>`;

  const chartRoot = root.querySelector("[data-review-chart]");
  if (!chartRoot || !window.LightweightCharts) return;
  const mini = LightweightCharts.createChart(chartRoot, {
    autoSize:true,
    height:340,
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
  line(summary.entry, "ВХОД", "#007aff");
  line(summary.initialStop, "СТОП", "#ff3b30");
  line(summary.target, "ЦЕЛЬ", "#34c759");
  line(summary.exit, "ВЫХОД", "#af52de");
  mini.timeScale().fitContent();
  reviewCharts.set(reviewId, mini);
}

async function loadTradeReviewDetail(reviewId) {
  if (!reviewId || selectedTradeReviewId !== reviewId) return;
  const inspector = $("tradeReviewInspector");
  const root = $("tradeReviewInspectorContent");
  if (!inspector || !root) return;
  inspector.classList.remove("hidden");

  const cached = tradeReviewCache.get(reviewId);
  if (cached) {
    renderTradeReviewDetail(reviewId, cached);
    return;
  }
  root.innerHTML = '<div class="empty-row">Загрузка разбора сделки…</div>';
  try {
    const sessionQuery = selectedReviewSession === "current"
      ? ""
      : `?session=${encodeURIComponent(selectedReviewSession)}`;
    const review = await api(`/api/reviews/trades/${encodeURIComponent(reviewId)}${sessionQuery}`);
    tradeReviewCache.set(reviewId, review);
    if (selectedTradeReviewId === reviewId) {
      renderTradeReviewDetail(reviewId, review);
    }
  } catch (error) {
    if (selectedTradeReviewId === reviewId) {
      root.innerHTML = `<div class="review-error">${String(error.message || error)}</div>`;
    }
  }
}

async function openTradeReview(reviewId) {
  if (!reviewId) return;
  selectedTradeReviewId = reviewId;
  const inspector = $("tradeReviewInspector");
  if (inspector) inspector.classList.remove("hidden");
  await loadTradeReviewDetail(reviewId);
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
    const selected = review && review.reviewId === selectedTradeReviewId;
    const reviewButton = review
      ? `<button class="button secondary review-open ${selected ? "active" : ""}" data-open-review="${review.reviewId}">${selected ? "Разбор открыт" : "Разбор сделки"}</button>`
      : '<span class="review-pending">Разбор формируется</span>';
    return `<article class="trade-card ${selected ? "review-selected" : ""}">
      <div class="trade-card-head">
        <div>
          <strong>${trade.symbol} · ${sideLabel(trade.side)}</strong>
          <span>${strategyLabel(trade.strategy)}</span>
        </div>
        <strong class="trade-card-net ${netClass}">${money(trade.netPnl)}</strong>
      </div>
      <div class="trade-card-metrics">
        <span><small>Вход → выход</small>${price(trade.entry)} → ${price(trade.exit)}</span>
        <span><small>Движение</small>${trade.exitMoveBps == null ? "—" : Number(trade.exitMoveBps).toFixed(1) + " bps"} · ${pct(trade.exitMovePct)}</span>
        <span><small>Первый тейк план</small>${bps(trade.plannedFirstTakeMovePct)}</span>
        <span><small>MFE</small>${trade.maxFavorableMoveBps == null ? "—" : Number(trade.maxFavorableMoveBps).toFixed(1) + " bps"} @ ${clock(trade.mfeAt)}</span>
        <span><small>MAE</small>${trade.maxAdverseMoveBps == null ? "—" : Number(trade.maxAdverseMoveBps).toFixed(1) + " bps"} @ ${clock(trade.maeAt)}</span>
        <span><small>Комиссии</small>${money(trade.fees)}</span>
        <span><small>Причина выхода</small>${reasonText(trade.reason)}</span>
      </div>
      <div class="trade-card-actions">${reviewButton}</div>
    </article>`;
  }).join("");
  document.querySelectorAll("[data-open-review]").forEach(button => {
    button.onclick = () => openTradeReview(button.dataset.openReview);
  });
}

function render(data) {
  const status = $("connection");
  const lossCap = data.risk?.sessionLossLimitEnabled ? "лимит убытка включён" : "исследование · лимит убытка выключен";
  const marketHealth = data.marketHealth || {};
  const marketReady = Boolean(marketHealth.ready);
  status.textContent = data.botRunning
    ? `PAPER · торговля включена · ${lossCap}`
    : marketReady
      ? `PAPER · готово · ${lossCap}`
      : marketHealth.scannerError
        ? "ОШИБКА РЫНОЧНЫХ ДАННЫХ"
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
  $("availableExposure").textContent = money(data.portfolio.availableNotional) + " · " + money(perPositionCap) + "/позицию";
  const minNetGate = data.risk.enforceMinNetProfitGate
    ? `net ≥ ${money(data.risk.minNetProfitUsd)}`
    : `net ${money(data.risk.minNetProfitUsd)} · наблюдение`;
  const rrGate = data.risk.enforceNetRewardRiskGate
    ? `R:R≥${Number(data.risk.minNetRewardRisk || 0).toFixed(2)}`
    : `R:R ${Number(data.risk.minNetRewardRisk || 0).toFixed(2)} · наблюдение`;
  const moveGate = data.risk.firstTakeMoveGateEnabled
    ? `первый тейк ≥ ${(Number(data.risk.minFirstTakeMovePct || 0) * 100).toFixed(2)}%`
    : `движение · наблюдение`;
  const winnerCostGate = data.risk.winnerCostShareGateEnabled
    ? `издержки winner ≤ ${(Number(data.risk.maxWinnerCostShare || 0) * 100).toFixed(0)}%`
    : `доля издержек · наблюдение`;
  $("costGate").textContent = `${moveGate} · ${winnerCostGate} · ${minNetGate} · ${rrGate}`;
  $("runTimer").textContent = data.botRunning
    ? duration(data.run?.remainingSeconds)
    : duration(data.run?.configuredDurationSeconds);
  $("sessionFile").textContent = data.sessionFile.split("/").pop();

  renderWorking(data.working);
  renderCandidates(data.candidates);
  renderStrategies(data.strategies);
  renderEvents(data.events);
  lastLiveClosedTrades = data.closedTrades;
  if (selectedReviewSession === "current" && data.closedTrades.length !== lastClosedTradeCount) {
    lastClosedTradeCount = data.closedTrades.length;
    if (!selectedTradeReviewId) {
      renderTrades(data.closedTrades);
    }
    api("/api/reviews/trades")
      .then(payload => {
        tradeReviewSummaries = payload.reviews || [];
        if (!selectedTradeReviewId) {
          renderTrades(data.closedTrades);
        }
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
    renderDomInspector(null);
    $("decisionStrip").innerHTML = "";
    renderPosition(null);
  }

  if (data.market) {
    selectedSymbol = data.market.symbol;
    const row = data.working.find(x => x.symbol === selectedSymbol);
    const position = row?.position || null;
    const book = data.market.orderbook;

    $("symbolTitle").textContent = data.market.symbol;
    renderSymbolMeta(data.market);
    $("trendBadge").textContent = trendLabel(data.market.trend);
    $("trendBadge").className = `trend ${data.market.trend}`;
    renderMarketChart(data.market, position);
    renderBook(book, data.market.densityContext);
    renderDomInspector(data.market.densityContext);
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
bindDomControls();
bindEventFilters();
bindReviewSessions();
const opportunityButton = $("opportunityRefresh");
if (opportunityButton) opportunityButton.onclick = loadOpportunityReview;
const tradeReviewClose = $("tradeReviewClose");
if (tradeReviewClose) tradeReviewClose.onclick = closeTradeReview;
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && selectedTradeReviewId) closeTradeReview();
});
refresh();
setInterval(refresh, 900);
