/*
  Минимальный JS сверх htmx/alpine-атрибутов в шаблонах. Вся интерактивность
  (открытие деталей, активный таб, tri-state радио "взято в работу") уже
  описана декларативно в HTML — сюда вынесено только то, что нельзя выразить
  атрибутами: глобальный тост на сетевые/серверные ошибки htmx-запросов.

  Ничего не знает о конкретных эндпоинтах API — общий обработчик на все
  hx-get/patch/delete в дашборде.
*/
(function () {
  function toast(message) {
    var root = document.getElementById("toast-root");
    if (!root) return;
    var el = document.createElement("div");
    el.className = "toast";
    el.setAttribute("role", "alert");
    el.textContent = message;
    root.appendChild(el);
    setTimeout(function () {
      el.remove();
    }, 4000);
  }

  document.body.addEventListener("htmx:responseError", function (evt) {
    var status = evt.detail && evt.detail.xhr ? evt.detail.xhr.status : "?";
    toast("Ошибка запроса (" + status + "). Попробуй ещё раз.");
  });

  document.body.addEventListener("htmx:sendError", function () {
    toast("Нет связи с сервером.");
  });
})();
