// Shared by the live chart, trade review and recorded replay (Lightweight Charts 4).
const CHART_COLORS = {
  up: "#57dcb1", down: "#ff8293", entry: "#7cb9ff", exit: "#c6a4ff",
  level: "#7790ae", trend: "#94a8c0", htf: "#bca0f5",
};

function chartThemeOptions() {
  const css = getComputedStyle(document.documentElement);
  const color = name => css.getPropertyValue(name).trim();
  return {
    layout: {
      background: {type: "solid", color: color("--surface")},
      textColor: color("--muted"), fontSize: 11,
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    },
    grid: {vertLines: {color: color("--chart-grid")}, horzLines: {color: color("--chart-grid")}},
    crosshair: {
      vertLine: {color: color("--border-strong"), labelBackgroundColor: color("--surface-raised")},
      horzLine: {color: color("--border-strong"), labelBackgroundColor: color("--surface-raised")},
    },
  };
}

function chartCandleOptions() {
  return {upColor: CHART_COLORS.up, downColor: CHART_COLORS.down, borderVisible: false,
    wickUpColor: CHART_COLORS.up, wickDownColor: CHART_COLORS.down};
}

function chartLocalTime(time, seconds = false) {
  return new Date(Number(time) * 1000).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", ...(seconds ? {second: "2-digit"} : {}),
  });
}

function chartPriceFormat(value) {
  const abs = Math.abs(Number(value) || 0);
  const precision = abs >= 1000 ? 2 : abs >= 100 ? 3 : abs >= 1 ? 4 : abs >= 0.1 ? 5 : abs >= 0.01 ? 6 : abs >= 0.001 ? 7 : 8;
  return {type: "price", precision, minMove: 10 ** -precision};
}

// Mark executions on their containing candle, never on a neighbouring candle
// when the execution is outside the loaded history or inside a data gap.
function markersOnCandles(candles, points, seconds = 60) {
  const times = candles.map(row => Number(row.time)).filter(Number.isFinite).sort((a, b) => a - b);
  return points.flatMap(point => {
    if (point.time == null || !Number.isFinite(Number(point.time))) return [];
    const timestamp = Number(point.time);
    let low = 0, high = times.length;
    while (low < high) {
      const mid = (low + high) >>> 1;
      if (times[mid] <= timestamp) low = mid + 1;
      else high = mid;
    }
    const time = times[low - 1];
    if (time == null || timestamp >= time + seconds) return [];
    return [{...point, time}];
  }).sort((a, b) => a.time - b.time);
}

function executionChartMarker(timestamp, value, side, closing = false, added = false) {
  const short = side === "short";
  const label = closing ? "ВЫХОД" : added ? "ДОБОР" : "ВХОД";
  const direction = short ? "ШОРТ" : "ЛОНГ";
  const formattedPrice = value == null || !Number.isFinite(Number(value)) ? "—"
    : Number(value).toLocaleString("en-US", {maximumFractionDigits: chartPriceFormat(value).precision});
  return {
    time: timestamp,
    position: closing ? (short ? "belowBar" : "aboveBar") : (short ? "aboveBar" : "belowBar"),
    shape: closing ? "circle" : (short ? "arrowDown" : "arrowUp"),
    color: closing ? CHART_COLORS.exit : CHART_COLORS.entry,
    size: 2,
    fillPrice: value,
    text: `${label}${closing ? "" : " " + direction} · ${chartLocalTime(timestamp, true)} · ${formattedPrice}`,
  };
}

function tradeChartMarkers(candles, trades, seconds = 60) {
  const points = trades.flatMap(trade => {
    const legs = trade.entryLegs || trade.entry_legs || [];
    const entries = legs.length > 1
      ? legs.map((leg, index) => executionChartMarker(leg.addedAt, leg.fill, trade.side, false, index > 0))
      : [executionChartMarker(trade.openedAt ?? trade.opened_at, trade.entry, trade.side)];
    if (trade.closedAt != null) entries.push(executionChartMarker(trade.closedAt, trade.exit, trade.side, true));
    return entries;
  });
  return markersOnCandles(candles, points, seconds);
}

// Labels identify the candle; dots identify the actual fill price on that candle.
// Separate lanes retain multiple executions on one candle without duplicate times
// in a chart series. Existing series are reused during live refreshes.
function addExecutionDots(chart) {
  const series = [];
  return {
    setMarkers(markers) {
      const lanes = [];
      for (const marker of markers) {
        if (marker.fillPrice == null || !Number.isFinite(Number(marker.fillPrice))) continue;
        let lane = lanes.find(row => row.color === marker.color && row.data.at(-1)?.time !== marker.time);
        if (!lane) { lane = {color: marker.color, data: []}; lanes.push(lane); }
        lane.data.push({time: marker.time, value: Number(marker.fillPrice)});
      }
      lanes.forEach((lane, index) => {
        if (!series[index]) series[index] = chart.addLineSeries({
          lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: 5,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
          autoscaleInfoProvider: () => null,
        });
        series[index].applyOptions({color: lane.color});
        series[index].setData(lane.data);
      });
      while (series.length > lanes.length) chart.removeSeries(series.pop());
    },
  };
}

function includeTradePrices(prices) {
  return original => {
    const info = original();
    if (!info?.priceRange) return info;
    const values = prices.filter(value => value != null && Number.isFinite(Number(value))).map(Number);
    return {...info, priceRange: {
      minValue: Math.min(info.priceRange.minValue, ...values),
      maxValue: Math.max(info.priceRange.maxValue, ...values),
    }};
  };
}

function addCandleVolume(chart, candles, legend) {
  candles.priceScale().applyOptions({scaleMargins: {top: 0.08, bottom: 0.25}});
  const volume = chart.addHistogramSeries({
    priceFormat: {type: "volume"}, priceScaleId: "volume",
    priceLineVisible: false, lastValueVisible: false,
  });
  volume.priceScale().applyOptions({scaleMargins: {top: 0.81, bottom: 0.01}, visible: false});
  let rowsByTime = new Map();
  let last = null;
  let label = "";
  let hovered = null;
  const number = value => value == null || !Number.isFinite(Number(value)) ? "—"
    : Number(value).toLocaleString(undefined, {maximumFractionDigits: 8});
  const amount = value => value == null || !Number.isFinite(Number(value)) ? "—"
    : new Intl.NumberFormat(undefined, {notation: "compact", maximumFractionDigits: 2}).format(Number(value));
  const show = row => {
    if (!legend) return;
    if (!row) { legend.textContent = "Объём: нет данных"; return; }
    const state = row.confirmed === false ? "формируется, объём неполный"
      : row.confirmed === true ? "закрыта" : "статус не записан";
    legend.textContent = `${label} · ${chartLocalTime(row.time, true)} · O ${number(row.open)} H ${number(row.high)} L ${number(row.low)} C ${number(row.close)} · Объём ${amount(row.volume)} · Оборот ${amount(row.turnover)} USDT · ${state}`;
  };
  chart.subscribeCrosshairMove(param => {
    hovered = param.time == null ? null : Number(param.time);
    show(rowsByTime.get(hovered) || last);
  });
  return {
    setData(rows, nextLabel = "") {
      label = nextLabel;
      rowsByTime = new Map(rows.map(row => [Number(row.time), row]));
      last = rows.at(-1) || null;
      volume.setData(rows.map(row => {
        const time = Number(row.time);
        if (row.volume == null || !Number.isFinite(Number(row.volume)) || Number(row.volume) < 0) return {time};
        return {time, value: Number(row.volume), color: Number(row.close) >= Number(row.open) ? CHART_COLORS.up + "66" : CHART_COLORS.down + "66"};
      }));
      show(rowsByTime.get(hovered) || last);
    },
  };
}
