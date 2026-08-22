/**
 * Форма отбора кандидатов на Google Apps Script.
 *
 * doGet отдаёт форму, doPost пишет анкету в лист «Данные» напрямую через
 * SpreadsheetApp — без сервисного аккаунта и без gspread: скрипт привязан к
 * таблице и работает от имени владельца.
 *
 * Порядок колонок, тексты вопросов,CSS и JS телеметрии не пишутся здесь
 * руками: Headers.gs, Questions.gs и Index.html собираются из HEADERS.py,
 * questions.json и templates/form.html скриптом apps-script/build_gs.py.
 *
 * Зона записи — A..AH плюс AS (флаги телеметрии) и AU (статус new).
 * Колонка AN (total_score) не трогается: там формула таблицы.
 */

var SHEET_NAME = 'Данные';
var HONEYPOT_FIELD = 'company_website';

// Пороги продублированы из app/config.py. Значения должны совпадать —
// расхождение означало бы, что одна и та же анкета получает разные флаги
// в зависимости от того, через какую форму она пришла.
var PASTE_PCT_THRESHOLD = 30;
var SPEED_CPM_THRESHOLD_DESKTOP = 400;
var SPEED_CPM_THRESHOLD_MOBILE = 700;
var SUSPICIOUS_MIN_LENGTH = 400;
var TAB_AWAY_SECONDS_THRESHOLD = 60;

var STATUS_NEW = 'new';
var STATUS_DUPLICATE = 'duplicate';
var TZ_OFFSET_HOURS = 3;

// ============================== doGet =====================================

function doGet() {
  var t = HtmlService.createTemplateFromFile('Index');
  t.QUESTIONS = QUESTIONS;
  t.honeypot = HONEYPOT_FIELD;
  t.formUrl = ScriptApp.getService().getUrl();
  return t.evaluate()
    .setTitle(QUESTIONS.title)
    .addMetaTag('viewport', 'width=device-width, initial-scale=1')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

// ============================== doPost ====================================

function doPost(e) {
  var params = (e && e.parameter) || {};

  // Приманка для ботов: поле скрыто стилями, человек его не видит.
  // Заполнено — отвечаем как обычно, но строку в таблице не занимаем.
  if (String(params[HONEYPOT_FIELD] || '').trim() !== '') {
    Logger.log('honeypot сработал — анкета не записана');
    return renderThanks();
  }

  var parsed = validate(params);
  if (parsed.errors.length) {
    Logger.log('анкета не прошла валидацию: ' + parsed.errors.join('; '));
    return renderError(parsed.errors.join('; '));
  }

  var telemetry = parseTelemetry(params.telemetry);
  var flags = telemetryFlags(telemetry);

  // Лог до записи: если SpreadsheetApp упадёт, анкету можно восстановить
  // из журнала выполнений.
  Logger.log('ПОЛУЧЕНА АНКЕТА ' + JSON.stringify({
    telegram: parsed.data.telegram, flags: flags
  }));

  try {
    writeSubmission(parsed.data, telemetry, flags);
  } catch (err) {
    Logger.log('не удалось записать анкету: ' + err);
    return renderError(
      'Не удалось сохранить анкету. Попробуйте отправить ещё раз через минуту.'
    );
  }
  return renderThanks();
}

// =========================== Валидация ====================================

var TELEGRAM_RE = /^@?[A-Za-z0-9_]{4,32}$/;
var URL_RE = /^https?:\/\/\S+\.\S+/i;

function validate(params) {
  var errors = [];
  var data = {};

  var fio = String(params.fio || '').replace(/\s+/g, ' ').trim();
  if (fio.length < 5 || fio.split(' ').length < 2) {
    errors.push('Укажите фамилию и имя полностью');
  } else if (fio.length > 200) {
    errors.push('Слишком длинное ФИО');
  }
  data.fio = fio;

  var tg = String(params.telegram || '').trim()
    .replace(/^(https?:\/\/)?(t\.me|telegram\.me)\//i, '');
  if (!TELEGRAM_RE.test(tg)) {
    errors.push('Telegram укажите в виде @username');
    data.telegram = tg;
  } else {
    data.telegram = '@' + tg.replace(/^@/, '');
  }

  var url = String(params.resume_url || '').trim();
  if (!URL_RE.test(url)) {
    errors.push('Ссылка на резюме должна начинаться с http:// или https://');
  } else if (url.length > 2000) {
    errors.push('Слишком длинная ссылка');
  }
  data.resume_url = url;

  data.pdn_consent = String(params.pdn_consent || '') !== '';
  if (!data.pdn_consent) {
    errors.push('Без согласия на обработку персональных данных анкету не принять');
  }

  data.stops = {};
  var missingStop = false;
  for (var i = 0; i < QUESTIONS.stop_questions.length; i++) {
    var field = QUESTIONS.stop_questions[i].field;
    var value = String(params[field] || '').trim();
    if (value !== 'Да' && value !== 'Нет') {
      missingStop = true;
    }
    data.stops[field] = value;
  }
  if (missingStop) {
    errors.push('Ответьте на все вопросы об условиях работы');
  }

  data.answers = {};
  for (var j = 0; j < QUESTIONS.open_questions.length; j++) {
    var oq = QUESTIONS.open_questions[j];
    var text = String(params[oq.field] || '').replace(/\s+/g, ' ').trim();
    data.answers[oq.field] = text;

    if (!text) {
      errors.push('Не заполнен ' + oq.label.toLowerCase());
      continue;
    }
    var min = oq.min_length || 0;
    if (text.length < min) {
      errors.push(
        oq.label + ': ответ слишком короткий (' + text.length +
        ' из ' + min + ' знаков минимум)'
      );
    }
    if (oq.max_length && text.length > oq.max_length) {
      errors.push(oq.label + ': не больше ' + oq.max_length + ' знаков');
    }
  }

  return { data: data, errors: errors };
}

// =========================== Телеметрия ===================================

function parseTelemetry(raw) {
  var t = { is_mobile: false, tab_switches: 0, total_time_sec: 0, fields: {} };
  try {
    var parsed = JSON.parse(raw || '{}');
    t.is_mobile = !!parsed.is_mobile;
    t.tab_switches = clamp(parsed.tab_switches, 0, 10000);
    t.total_time_sec = clamp(parsed.total_time_sec, 0, 86400);
    var fields = parsed.fields || {};
    for (var key in fields) {
      if (!fields.hasOwnProperty(key)) continue;
      var f = fields[key] || {};
      var length = clamp(f.length, 0, 1000000);
      t.fields[key] = {
        length: length,
        pasted_chars: clamp(f.pasted_chars, 0, length),
        speed_cpm: clamp(f.speed_cpm, 0, 100000),
        backspaces: clamp(f.backspaces, 0, 1000000),
        away_before_input_sec: clamp(f.away_before_input_sec, 0, 86400)
      };
    }
  } catch (err) {
    // Телеметрия приходит от кандидата. Кривая не должна стоить ему анкеты.
    Logger.log('телеметрия не разобралась, продолжаю с пустой: ' + err);
  }
  return t;
}

function clamp(value, lo, hi) {
  var n = Number(value);
  if (!isFinite(n)) return lo;
  return Math.max(lo, Math.min(n, hi));
}

function fieldTelemetry(t, name) {
  return t.fields[name] || {
    length: 0, pasted_chars: 0, speed_cpm: 0, backspaces: 0, away_before_input_sec: 0
  };
}

function pastePct(m) {
  if (!m.length) return 0;
  return Math.min(100, Math.round(m.pasted_chars / m.length * 1000) / 10);
}

/** Повторяет app/checks.py: telemetry_flags. Мобильные проверяются мягче. */
function telemetryFlags(t) {
  var flags = [];
  if (t.is_mobile) flags.push('MOBILE');
  var speedLimit = t.is_mobile
    ? SPEED_CPM_THRESHOLD_MOBILE
    : SPEED_CPM_THRESHOLD_DESKTOP;

  for (var i = 0; i < QUESTIONS.open_questions.length; i++) {
    var m = fieldTelemetry(t, QUESTIONS.open_questions[i].field);

    if (pastePct(m) > PASTE_PCT_THRESHOLD) {
      addFlag(flags, 'PASTED');
    } else if (m.speed_cpm > speedLimit) {
      addFlag(flags, 'PASTED');
    }

    // На мобильных keydown по Backspace приходит нестабильно — правило снято.
    if (!t.is_mobile && m.backspaces === 0 && m.length > SUSPICIOUS_MIN_LENGTH) {
      addFlag(flags, 'SUSPICIOUS');
    }

    if (m.away_before_input_sec > TAB_AWAY_SECONDS_THRESHOLD) {
      addFlag(flags, 'TAB_SWITCH');
    }
  }
  return flags;
}

function addFlag(flags, flag) {
  if (flags.indexOf(flag) === -1) flags.push(flag);
}

// ============================= Запись =====================================

function getSheet() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
  if (!sheet) {
    throw new Error('в таблице нет листа «' + SHEET_NAME + '»');
  }
  return sheet;
}

function writeSubmission(data, telemetry, flags) {
  // Блокировка на весь цикл «найти строку — записать»: без неё две
  // одновременные отправки получили бы один и тот же номер строки.
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var sheet = getSheet();
    var duplicate = telegramExists(sheet, data.telegram);
    var row = allocateRow(sheet);

    sheet.getRange(row, 1, 1, COL_FORM_END)
      .setValues([buildFormRow(data, telemetry)]);
    sheet.getRange(row, COL_FLAGS).setValue(flags.join(', '));
    sheet.getRange(row, COL_STATUS)
      .setValue(duplicate ? STATUS_DUPLICATE : STATUS_NEW);

    if (duplicate) {
      Logger.log('строка ' + row + ': дубль по телеграму ' + data.telegram);
    } else {
      Logger.log('анкета записана в строку ' + row + ', флаги: ' + (flags.join(', ') || 'нет'));
    }
    SpreadsheetApp.flush();
  } finally {
    lock.releaseLock();
  }
}

/**
 * Следующая свободная строка — по колонке A.
 *
 * getLastRow() здесь не годится: формула в AN размечена на 300 строк вперёд,
 * и для таблицы эти строки не пустые — getLastRow() вернул бы 301 сразу.
 */
function allocateRow(sheet) {
  var maxRows = sheet.getMaxRows();
  var column = sheet.getRange(1, 1, maxRows, 1).getValues();
  var last = 0;
  for (var i = 0; i < column.length; i++) {
    if (String(column[i][0]).trim() !== '') last = i + 1;
  }
  var row = Math.max(last + 1, FIRST_DATA_ROW);

  if (row > FORMULA_TOPUP_FROM_ROW) {
    ensureTotalFormula(sheet, row);
  }
  return row;
}

/** Дописывает формулу total_score в AN — так лимит в 300 строк снимается сам. */
function ensureTotalFormula(sheet, row) {
  if (row > LAST_PREPARED_ROW) {
    Logger.log('строка ' + row + ' вне размеченных ' + LAST_PREPARED_ROW +
               ' — формула total_score дописывается скриптом');
  }
  try {
    sheet.getRange(row, COL_TOTAL_SCORE)
      .setFormula(TOTAL_SCORE_FORMULA.replace(/\{row\}/g, String(row)));
  } catch (err) {
    Logger.log('не удалось дописать формулу total_score в строку ' + row + ': ' + err);
  }
}

function telegramExists(sheet, telegram) {
  var needle = String(telegram).replace(/^@/, '').toLowerCase();
  if (!needle) return false;
  var maxRows = sheet.getMaxRows();
  var column = sheet.getRange(1, COL_TELEGRAM, maxRows, 1).getValues();
  for (var i = FIRST_DATA_ROW - 1; i < column.length; i++) {
    if (String(column[i][0]).trim().replace(/^@/, '').toLowerCase() === needle) {
      return true;
    }
  }
  return false;
}

/** 34 значения зоны A..AH, строго в порядке HEADERS. */
function buildFormRow(data, telemetry) {
  var row = [
    Utilities.getUuid().replace(/-/g, '').substring(0, 12),
    Utilities.formatDate(
      new Date(), 'GMT+' + TZ_OFFSET_HOURS, 'yyyy-MM-dd HH:mm:ss'
    ),
    data.fio,
    data.telegram,
    data.resume_url,
    data.pdn_consent ? 'Да' : 'Нет'
  ];

  var stops = QUESTIONS.stop_questions;
  for (var i = 0; i < stops.length; i++) row.push(data.stops[stops[i].field] || '');

  var open = QUESTIONS.open_questions;
  for (var j = 0; j < open.length; j++) row.push(data.answers[open[j].field] || '');
  for (var k = 0; k < open.length; k++) {
    row.push(pastePct(fieldTelemetry(telemetry, open[k].field)));
  }
  for (var m = 0; m < open.length; m++) {
    row.push(Math.round(fieldTelemetry(telemetry, open[m].field).speed_cpm * 10) / 10);
  }
  for (var n = 0; n < open.length; n++) {
    row.push(fieldTelemetry(telemetry, open[n].field).backspaces);
  }

  row.push(telemetry.tab_switches);
  row.push(Math.round(telemetry.total_time_sec));

  if (row.length !== COL_FORM_END) {
    throw new Error('ожидалось ' + COL_FORM_END + ' значений, собрано ' + row.length);
  }
  return row;
}

// ============================== Ответы ====================================

function renderThanks() {
  return page(
    'Анкета отправлена',
    '<h1>Спасибо, анкета отправлена</h1>' +
    '<p>Мы изучим ваши ответы и свяжемся с вами в Telegram.</p>' +
    '<p>Отправлять анкету повторно не нужно.</p>'
  );
}

function renderError(message) {
  return page(
    'Проверьте анкету',
    '<h1>Анкета не отправлена</h1>' +
    '<p class="err">' + escapeHtml(message) + '</p>' +
    '<p><a href="javascript:history.back()">Вернуться и исправить</a></p>'
  );
}

function page(title, body) {
  var html =
    '<!doctype html><html lang="ru"><head><base target="_top"><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<title>' + escapeHtml(title) + '</title><style>' +
    'body{margin:0;padding:24px 16px;background:#f5f5f4;color:#1c1917;' +
    'font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;' +
    'display:flex;align-items:center;justify-content:center;min-height:100vh}' +
    '.card{max-width:700px;width:100%;background:#fff;border:1px solid #d6d3d1;' +
    'border-radius:8px;padding:40px 32px;text-align:center}' +
    'h1{font-size:24px;margin:0 0 12px;font-weight:650}' +
    'p{color:#57534e;margin:0 0 8px}p:last-child{margin-bottom:0}' +
    '.err{color:#991b1b}a{color:#1c1917}' +
    '@media(max-width:560px){.card{padding:28px 20px}h1{font-size:21px}}' +
    '</style></head><body><div class="card">' + body + '</div></body></html>';

  return HtmlService.createHtmlOutput(html)
    .setTitle(title)
    .addMetaTag('viewport', 'width=device-width, initial-scale=1')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ========================== Самопроверка ==================================

/**
 * Запустите вручную из редактора после первой установки.
 * Проверяет доступ к листу и совпадение шапки с HEADERS.
 */
function checkSetup() {
  var sheet = getSheet();
  var actual = sheet.getRange(1, 1, 1, HEADERS.length).getValues()[0];
  var problems = [];
  for (var i = 0; i < HEADERS.length; i++) {
    if (String(actual[i]).trim() !== HEADERS[i]) {
      problems.push(
        'колонка ' + (i + 1) + ': ожидалось «' + HEADERS[i] +
        '», в таблице «' + actual[i] + '»'
      );
    }
  }
  if (problems.length) {
    Logger.log('ШАПКА РАСХОДИТСЯ:\n' + problems.join('\n'));
  } else {
    Logger.log('шапка листа совпадает с HEADERS (' + HEADERS.length + ' колонок)');
  }
  Logger.log('следующая свободная строка: ' + allocateRow(sheet));
  Logger.log('адрес формы: ' + ScriptApp.getService().getUrl());
  return problems.length === 0;
}
