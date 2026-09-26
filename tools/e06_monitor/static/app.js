'use strict';
const $ = id => document.getElementById(id);
const labels = {baseline: 'Контроль', candidate: 'Кандидат'};
const phaseLabels = {smoke: 'Технический прогон · 30 минут', 'smoke-replay': 'Проверка воспроизведения', independent: 'Независимая запись · 12 часов', capture: 'Парная запись'};
const statusLabels = {waiting: 'Ожидание записи', warming: 'Прогрев / загрузка истории', recording: 'Есть свежие записи', stale: 'Нет свежих записей', recorded: 'Запись периода завершена', replay: 'Этап воспроизведения', replay_recorded: 'Воспроизведение завершено', completed: 'Прогон завершён', failed: 'Прогон остановлен с ошибкой', monitor_error: 'Ошибка чтения журнала'};
const strategies = {level_breakout: 'Пробой', weak_level_rejection: 'Отбой от уровня', orderbook_density: 'Плотность'};
const reasons = {stop: 'Стоп', target: 'Цель', runner_target: 'Цель остатка', no_follow_through: 'Нет продолжения', run_end: 'Конец прогона'};
let state = null;
const esc = v => String(v ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const valid = v => typeof v === 'number' && Number.isFinite(v);
const fmt = (v, digits = 2) => valid(v) ? v.toLocaleString('ru-RU', {minimumFractionDigits: digits, maximumFractionDigits: digits}) : '—';
const price = v => valid(v) ? v.toLocaleString('ru-RU', {maximumFractionDigits: 8}) : '—';
const tone = v => valid(v) && v !== 0 ? (v > 0 ? 'positive' : 'negative') : '';
const money = v => `<span class="${tone(v)}">${valid(v) && v > 0 ? '+' : ''}${fmt(v)}</span>`;
const at = v => valid(v) ? new Date(v * 1000).toLocaleTimeString('ru-RU') : '—';
function duration(v) { if (!valid(v)) return '—'; const s = Math.floor(Math.max(0, v)); return `${String(Math.floor(s/3600)).padStart(2,'0')}:${String(Math.floor(s/60)%60).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`; }
function metric(label, value, note = '') { return `<div class="metric"><label>${label}</label><strong>${value}</strong>${note ? `<small>${note}</small>` : ''}</div>`; }

function render(s) {
  state = s;
  const status = s.status;
  const error = status === 'failed' || status === 'monitor_error';
  const live = status === 'recording';
  $('connection').textContent = statusLabels[status] || status;
  $('connection').className = `pill ${error ? 'error' : live ? 'live' : ''}`;
  $('updated').textContent = s.latestAt ? `Последняя запись ${at(s.latestAt)} · ${Math.round(s.ageSeconds)} с назад` : 'Обновление каждые 2 секунды';
  $('phase-title').textContent = phaseLabels[s.phase] || 'Ожидание нового прогона';
  const timing = valid(s.elapsedSeconds) && valid(s.durationSeconds);
  $('timing').textContent = timing ? `${duration(s.elapsedSeconds)} / ${duration(s.durationSeconds)}` : 'Таймер появится после старта';
  $('progress').value = timing ? Math.min(100, s.elapsedSeconds / s.durationSeconds * 100) : 0;
  $('capture').textContent = `Каталог записи: ${s.capture}`;
  $('phases').innerHTML = (s.phases || []).map((p, i) => `<span class="phase ${p.status === 'failed' ? 'failed' : p.status === 'recorded' ? 'done' : p.name === s.phase ? 'active' : ''}">${p.status === 'recorded' ? '✓' : i+1} ${esc(phaseLabels[p.name])}${p.audit ? ' · аудит сохранён' : ''}</span>`).join('');
  let notice = 'Ожидаем новый прогон. Запустите запись с указанным каталогом — данные появятся автоматически.';
  if (status === 'recording') notice = 'Поступают записи paper-прогона на рыночном потоке Bybit. Контроль и кандидат работают на общих входных данных.';
  if (status === 'warming') notice = 'Каталог создан. Ожидаем готовность рыночных данных и читаем начало журнала.';
  if (status === 'stale') notice = 'Новых записей больше 30 секунд нет. Проверьте терминал прогона: процесс мог остановиться. Монитор показывает последние сохранённые данные.';
  if (status === 'recorded') notice = 'Торговый период записан. Финальный статус всей цепочки ещё не сохранён; возможен аудит. Наличие результата само по себе не подтверждает успешное завершение.';
  if (status === 'replay' || status === 'replay_recorded') notice = 'Создан этап проверки воспроизведения smoke. Ниже — результаты smoke. При старте независимого периода монитор переключится автоматически. Журнал replay не содержит отдельного индикатора активности процесса.';
  if (status === 'completed') notice = 'Запись завершена. Показаны сохранённые результаты; заключение по E06 требует отдельного анализа и полного replay независимого периода.';
  if (status === 'failed') notice = `Прогон остановлен: ${s.failure?.errorType || ''}: ${s.failure?.error || 'Причина указана в failure.json'}. Эти данные не считаются завершённым независимым прогоном.`;
  if (status === 'monitor_error') notice = `Монитор не может продолжить чтение: ${s.monitorError}. Состояние процесса прогона проверяйте в его терминале.`;
  $('alert').textContent = notice;
  $('alert').className = `notice ${error || status === 'stale' ? 'error' : live || status === 'completed' ? 'good' : ''}`;
  const ports = s.portfolios || {};
  $('portfolios').innerHTML = ['baseline','candidate'].map(name => {
    const p = ports[name] || {}, complete = p.history?.complete;
    const rule = s.experiment === 'E06' ? (name === 'baseline' ? 'Подтверждение пробоя: 8 секунд' : 'E06: 3 секунды при подтверждённой структуре, иначе 8') : 'Парный портфель · параметры сохранены в записи';
    return `<article class="card portfolio ${name}"><h2>${labels[name]}</h2><p class="subtitle">${rule}</p><div class="metrics">${metric('Капитал, USDT', fmt(p.equity), `Баланс ${fmt(p.balance)}`)}${metric('Изменение капитала', money(p.net), 'От начального баланса')}${metric('Открытый PnL', money(p.unrealized), 'Оценка paper-брокера')}</div><div class="portfolio-bottom"><span>Открыто <b>${esc(p.openPositions)}</b></span><span>Закрыто <b>${p.closedCount ?? '—'}${p.history && !complete ? ' · загрузка' : ''}</b></span><span>Net закрытых ${money(complete ? p.closedNet : null)}</span></div></article>`;
  }).join('');
  const b = ports.baseline?.equity, c = ports.candidate?.equity;
  $('delta').innerHTML = `${money(valid(b) && valid(c) ? c-b : null)} <small>USDT</small>`;
  $('history').textContent = Object.entries(ports).filter(([,p]) => !p.history?.complete).map(([n,p]) => `${labels[n]}: загрузка ${p.history?.percent || 0}%`).join(' · ');
  const positions = Object.entries(ports).flatMap(([name,p]) => (p.positions || []).map(pos => ({...pos, portfolio:name})));
  $('positions').innerHTML = positions.map(p => `<tr><td>${labels[p.portfolio]}</td><td><b>${esc(p.symbol)}</b></td><td>${esc(p.side?.toUpperCase())}</td><td>${price(p.entry)}</td><td>${price(p.stop)} / ${price(p.target)}</td><td>${fmt(p.notional)}</td><td>${money(p.unrealized_pnl)}</td><td>${at(p.observedAt)}</td></tr>`).join('') || `<tr><td colspan="8" class="empty">${!Object.keys(ports).length ? 'Ожидание записи' : Object.values(ports).some(p => !p.history?.complete) ? 'Детали позиций появятся после загрузки истории' : Object.values(ports).some(p => p.openPositions > 0) ? 'Ожидаем снимки открытых позиций из журнала' : 'Открытых позиций нет'}</td></tr>`;
  renderTrades();
  $('health').innerHTML = ['baseline','candidate'].map(name => {
    const h = ports[name]?.marketHealth, historical = !live;
    const badge = !h ? 'Нет данных' : h.ready ? 'Данные готовы' : 'Входы ограничены';
    return `<div><h3>${labels[name]} · <span class="${h?.ready ? 'positive' : ''}">${badge}${historical && h ? ' (последний снимок)' : ''}</span></h3><p>Активных инструментов: ${esc(h?.activeSymbolCount)} · Живой поток: ${esc(h?.liveSymbolCount)} · Готовых стаканов: ${esc(h?.fastBookReadyCount)}</p><p>Часы биржи: ${!h?.clock ? '—' : h.clock.valid ? 'готовы' : 'не готовы'} · Неопределённость: ${fmt(h?.clock?.uncertainty_ms,1)} мс</p><p class="reason">${esc(h?.reason || h?.clock?.reason || (h ? 'Блокировок на момент снимка нет' : 'Ожидание записи'))}</p></div>`;
  }).join('');
  $('chart-note').textContent = `Баланс + открытый PnL по оценке paper-брокера · USDT${s.equityHistory && !s.equityHistory.complete ? ` · загрузка истории ${s.equityHistory.percent}%` : ''}${s.phase === 'smoke-replay' ? ' · данные smoke' : ''}`;
  drawChart();
}

function renderTrades() {
  const selected = $('trade-filter').value;
  const trades = Object.entries(state?.portfolios || {}).filter(([n]) => selected === 'all' || selected === n)
    .flatMap(([name,p]) => (p.closedTrades || []).map(t => ({...t,portfolio:name}))).sort((a,b) => b.closedAt-a.closedAt);
  $('trades').innerHTML = trades.map(t => `<tr><td>${at(t.closedAt)}</td><td>${labels[t.portfolio]}</td><td><b>${esc(t.symbol)}</b></td><td>${esc(strategies[t.strategy] || t.strategy)}</td><td>${esc(t.side?.toUpperCase())}</td><td>${price(t.entry)} → ${price(t.exit)}</td><td>${fmt(t.fees)}</td><td>${money(t.netPnl)}</td><td>${esc(reasons[t.reason] || t.reason)}</td></tr>`).join('') || '<tr><td colspan="9" class="empty">Закрытых сделок пока нет</td></tr>';
}

function drawChart() {
  const canvas = $('chart'), rect = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width*dpr); canvas.height = Math.round(rect.height*dpr);
  const ctx = canvas.getContext('2d'); ctx.scale(dpr,dpr);
  const points = (state?.chart || []).filter(p => valid(p.time) && valid(p.baseline) && valid(p.candidate));
  $('chart-empty').classList.toggle('hidden', points.length > 1);
  if (points.length < 2) return;
  const left=62,right=10,top=14,bottom=29,w=rect.width-left-right,h=rect.height-top-bottom;
  const ys=points.flatMap(p => [p.baseline,p.candidate]), min=Math.min(...ys),max=Math.max(...ys),pad=Math.max((max-min)*.15,.1);
  const lo=min-pad,hi=max+pad,t0=points[0].time,t1=points[points.length-1].time;
  const x=t => left+(t-t0)/Math.max(1,t1-t0)*w, y=v => top+(hi-v)/(hi-lo)*h;
  ctx.font='11px Segoe UI, sans-serif';ctx.fillStyle='#87909d';ctx.textAlign='right';
  for(let i=0;i<=4;i++){const v=lo+(hi-lo)*i/4,yy=y(v);ctx.strokeStyle='#edf0f4';ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(rect.width-right,yy);ctx.stroke();ctx.fillText(fmt(v),left-10,yy+4);}
  for(const [name,color] of [['baseline','#4969cf'],['candidate','#8b60c9']]){ctx.beginPath();ctx.strokeStyle=color;ctx.lineWidth=2;ctx.setLineDash(name==='candidate'?[5,3]:[]);points.forEach((p,i)=>i?ctx.lineTo(x(p.time),y(p[name])):ctx.moveTo(x(p.time),y(p[name])));ctx.stroke();}ctx.setLineDash([]);
  ctx.textAlign='left';ctx.fillText(at(t0),left,rect.height-5);ctx.textAlign='right';ctx.fillText(at(t1),rect.width-right,rect.height-5);
}

async function poll() {
  try { const response = await fetch('/api/state', {cache:'no-store', signal:AbortSignal.timeout(5000)}); if(!response.ok) throw new Error(`HTTP ${response.status}`); render(await response.json()); }
  catch(e) { $('connection').textContent='Нет связи с монитором';$('connection').className='pill error';$('alert').className='notice error';$('alert').textContent='UI не получает обновления. Проверьте процесс монитора; состояние самого прогона неизвестно. На экране могут оставаться старые данные.'; }
  finally { setTimeout(poll,2000); }
}
$('trade-filter').addEventListener('change',renderTrades);
window.addEventListener('resize',drawChart);
poll();
