// Отправка для статической версии на своём домене.
//
// Страница лежит на чужом для Apps Script origin, поэтому запрос обязан
// быть «простым» — таким, который браузер шлёт без предварительного OPTIONS.
// Apps Script на OPTIONS не отвечает и заголовков CORS не выдаёт, так что
// preflight убил бы отправку ещё до неё. Простым POST делает Content-Type:
// у text/plain preflight не требуется. Тело при этом остаётся JSON-строкой
// — её разбирает doPost, см. parseBody в Code.gs.
//
// google.script.run здесь нет и быть не может: он существует только внутри
// страницы, отданной самим Apps Script.
var SUBMIT_URL = "__SUBMIT_URL__";

window.submitPayload = function (data) {
  if (!SUBMIT_URL) {
    // Страницу собрали, не зная адреса приёма. Молчать нельзя: кандидат
    // иначе решит, что анкета ушла.
    return Promise.resolve({
      ok: false,
      errors: {
        _form: "Страница собрана без адреса приёма анкеты. " +
               "Сообщите об этом, пожалуйста, тому, кто прислал ссылку."
      }
    });
  }

  return fetch(SUBMIT_URL, {
    method: "POST",
    // Заголовок ровно один и именно такой — любой другой тип содержимого
    // (включая application/json) заставит браузер сделать preflight.
    headers: { "Content-Type": "text/plain;charset=utf-8" },
    body: JSON.stringify(data),
    // Apps Script отвечает редиректом на googleusercontent — по нему и
    // приходит тело с заголовком, разрешающим чтение ответа.
    redirect: "follow"
  }).then(function (r) {
    // Ответ может оказаться не JSON: страница входа Google, если доступ к
    // развёртыванию закрыт, или HTML ошибки. Разбор не должен ронять
    // отправку в консоль без объяснения.
    return r.json().catch(function () {
      return {
        ok: false,
        errors: {
          _form: "Анкета не отправлена: сервер ответил неожиданным образом. " +
                 "Попробуйте ещё раз через минуту."
        }
      };
    });
  });
};
