// Display labels only. The broker's original reason remains in the session log.
const EXIT_REASON_LABELS = Object.freeze({
  stop: "стоп",
  target: "цель",
  runner_target: "цель оставшейся части позиции",
  no_follow_through: "нет продолжения движения",
  breakout_failed_back_inside: "пробой не удержался: цена вернулась за уровень",
  breakout_context_lost: "рыночный контекст больше не поддерживает пробой",
  weak_level_invalidated: "отбой отменён: цена закрепилась за границей уровня",
  weak_level_context_lost: "рыночный контекст больше не поддерживает отбой",
  trend_structure_context_lost: "рыночный контекст больше не поддерживает трендовую сделку",
  density_price_flow_invalidated: "цена и поток сделок отменили сигнал плотности",
  density_context_lost: "тренд больше не поддерживает сделку от плотности",
  duration_elapsed: "время прогона истекло",
  bot_stop: "остановлено пользователем",
  shutdown: "завершение приложения",
});

function exitReasonText(value) {
  const code = String(value ?? "").trim();
  if (!code) return "Причина не записана";
  return Object.hasOwn(EXIT_REASON_LABELS, code)
    ? EXIT_REASON_LABELS[code]
    : `Неизвестная причина: ${code}`;
}

function exitReasonHtml(value) {
  const escape = text => String(text).replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
  const code = String(value ?? "").trim();
  return `<span title="Код причины: ${escape(code || "не записан")}">${escape(exitReasonText(value))}</span>`;
}
