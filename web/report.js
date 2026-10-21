async function loadReport() {
  const note = document.getElementById("snapshot");
  try {
    const response = await fetch(location.origin + "/files/part2.json", {cache: "no-store"});
    if (!response.ok) throw new Error("Нет сохранённого файла");
    const rows = await response.json();
    const body = document.getElementById("saved-results");
    for (const r of rows) {
      const tr = body.insertRow();
      tr.insertCell().textContent = new URL(r.url).hostname;
      const badge = document.createElement("span"); badge.className = "st st" + r.status;
      badge.textContent = ["0 Не подтверждено", "1 Найдено", "2 Проверить"][r.status];
      tr.insertCell().append(badge); tr.insertCell().textContent = r.reason;
    }
    const dates = rows.map(function(r) { return r.checked_at; }).filter(Boolean).sort();
    note.textContent = dates.length ? "Сохранённая проверка: " + new Date(dates[dates.length - 1]).toLocaleString("ru-RU") + ". Это не новый запуск." : "Сохранённые результаты предыдущего прогона.";
  } catch (error) { note.textContent = "Не удалось загрузить сохранённый результат. Скачайте таблицу или запустите проверку ссылок."; }
}
loadReport();
